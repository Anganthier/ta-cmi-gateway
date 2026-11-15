"""Persistent history store (SQLite) for the CMI gateway (Sprint S2-1).

The gateway keeps a bounded, append-only history of live-poll samples in
``data/history.db`` (git-ignored).  WinSol history lives in the canonical
Parquet store ``data/winsol_pq/`` and is served on demand
(see :mod:`app.winsol_wide`) -- it is never merged into this DB.  This
module provides:

* :class:`HistoryDB` -- a WAL-mode, lock-protected SQLite wrapper that is safe
  to call from the single FastAPI event loop (and, via ``check_same_thread=
  False`` + a ``threading.Lock``, from worker threads such as
  :func:`asyncio.to_thread`).
* idempotent schema init/migration (``CREATE ... IF NOT EXISTS`` + missing-column
  ``ALTER``), so the file can be opened repeatedly across restarts.
* an idempotent upsert helper (``INSERT OR IGNORE``) and fast, indexed
  range/series queries for the ``/api/v1/history`` route.
* a retention cleanup that keeps the file bounded (``HISTORY_RETENTION_DAYS``).

The ``measurements`` schema holds only live-polling rows
(``schematic`` / ``json:<n>``); the WinSol series are served on demand from
the Parquet store instead, so the two stores never mix.

No network access happens here; this module only touches the local DB file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.cmi.json_api import JsonApiResult
    from app.cmi.schematic_parser import SchematicPage

logger = logging.getLogger("cmi.history")

#: Column layout of the canonical ``measurements`` table.  ``ts`` uses the
#: canonical ``YYYY-MM-DD HH:MM:SS`` string format shared with the WinSol
#: Parquet store (``data/winsol_pq/``), so the two stores stay comparable.
_MEASUREMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ts", "TEXT"),
    ("device", "TEXT"),
    ("channel", "INTEGER"),
    ("ad", "TEXT"),
    ("value", "REAL"),
    ("unit", "TEXT"),
    ("source", "TEXT"),
)

#: The composite key that makes upserts idempotent.
_MEASUREMENT_PK: tuple[str, ...] = ("ts", "device", "channel", "ad")

#: Upper bound for a single history query (defence against runaway series).
MAX_HISTORY_LIMIT = 100_000
DEFAULT_HISTORY_LIMIT = 5_000

#: Default look-back window when the caller does not pass ``start``/``end``.
DEFAULT_RANGE_HOURS = 24.0

#: Supported aggregation functions (whitelisted so user input never reaches SQL).
_ALLOWED_AGGS: frozenset[str] = frozenset({"last", "min", "max", "avg"})

#: Sources pruned by retention. DECISION (human-approved): retention applies
#: ONLY to live-polling sources; historical / backfilled sources (e.g.
#: ``winsol``) are EXEMPT and are never deleted by the retention cleanup.
DEFAULT_PRUNE_SOURCES: frozenset[str] = frozenset({"schematic", "json"})

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_iso() -> str:
    """Current local wall-clock time as the canonical ``YYYY-MM-DD HH:MM:SS``.

    All timestamps in the DB use this naive local format (the CMI's own clock),
    so retention / range math uses naive local datetimes to stay consistent.
    """
    return datetime.now().strftime(_TS_FMT)


def parse_ts(value: str) -> str:
    """Normalise a user-supplied timestamp to ``YYYY-MM-DD HH:MM:SS``.

    Accepts either ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS``.  Raises
    :class:`ValueError` when the value does not parse.
    """
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime(_TS_FMT)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp {value!r} (use YYYY-MM-DD[ HH:MM:SS])")


# ---------------------------------------------------------------------------
# Row builders (poller -> measurements rows)
# ---------------------------------------------------------------------------
def schematic_rows(page: SchematicPage, ts: str) -> list[tuple[str, str, int, str, float, str | None, str]]:
    """Turn one parsed schematic page into ``measurements`` rows.

    Only value blocks that carry a human label **and** a numeric value are
    persisted (the S2-1 persistence rule).  ``device`` is the constant
    ``'schematic'``; ``channel`` is the deterministic ``page*100+pos``; ``ad``
    is ``'A'`` for measurements / ``'D'`` for digital-state blocks.
    """
    rows: list[tuple[str, str, int, str, float, str | None, str]] = []
    for pos in page.positions:
        if not pos.label or pos.value is None:
            continue
        ad = "D" if pos.pos_type in ("mode_select", "switch") else "A"
        rows.append((ts, "schematic", page.page * 100 + pos.index, ad, pos.value, pos.unit, "schematic"))
    return rows


def json_rows(result: JsonApiResult, ts: str) -> list[tuple[str, str, int, str, float, str | None, str]]:
    """Turn one JSON-API node snapshot into ``measurements`` rows.

    ``device`` is ``'json:<node>'``; ``channel`` is the CMI channel ``Number``;
    ``ad`` is the CMI ``A``/``D`` flag; every measurable value is persisted.
    """
    device = f"json:{result.node}"
    rows: list[tuple[str, str, int, str, float, str | None, str]] = []
    for _section, entries in result.values.items():
        for entry in entries:
            if entry.value.value is None:
                continue
            rows.append(
                (ts, device, entry.number, entry.ad, entry.value.value,
                 entry.value.unit_symbol, "json")
            )
    return rows


@dataclass
class HistoryQuery:
    """A fully-resolved request for the ``/api/v1/history`` endpoint."""

    start: str
    end: str
    device: str | None = None
    channels: tuple[int, ...] = ()
    metric: str | None = None  # maps to the ``source`` column
    #: A/D filter on the ``ad`` column: ``"A"`` | ``"D"``; ``None`` -> no filter
    #: (the pre-S4-4 contract: a device/channel series unions A and D rows).
    ad: str | None = None
    agg: str = "last"
    interval: int | None = None
    limit: int = DEFAULT_HISTORY_LIMIT


class HistoryDB:
    """WAL-mode SQLite history store, safe for the FastAPI event loop.

    A single :class:`sqlite3.Connection` is opened lazily and guarded by a
    :class:`threading.Lock`; every public method is a short, lock-held call so
    it never blocks the event loop for more than a few milliseconds.  The
    connection is created with ``check_same_thread=False`` so the same object
    can also be driven from worker threads if needed.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # -- connection lifecycle -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        from urllib.parse import quote

        # ``uri=True`` requires a ``file:`` URI.  A bare ``file:<abs-path>``
        # (no ``mode=``) both creates the file on first open and preserves it
        # afterwards; the path is percent-encoded so spaces/quotes survive.
        abspath = str(self._path.resolve())
        uri = "file:" + quote(abspath)
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> HistoryDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema ---------------------------------------------------------------
    def init_schema(self) -> None:
        """Create the canonical tables/indexes if absent (idempotent).

        Also migrates a pre-existing ``measurements`` table by adding any
        missing canonical column, so an older file is upgraded in place.
        """
        with self._lock:
            conn = self._ensure_conn()
            cols_sql = ",\n    ".join(f"{name} {decl}" for name, decl in _MEASUREMENT_COLUMNS)
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS measurements (\n    {cols_sql},\n"
                f"    PRIMARY KEY ({', '.join(_MEASUREMENT_PK)})\n)"
            )
            # Migration FIRST: add any canonical column that is missing from a
            # pre-existing table, so the index definitions below can always be
            # built (creating an index on a not-yet-existing column would fail).
            existing = {row[1] for row in conn.execute("PRAGMA table_info(measurements)")}
            for name, decl in _MEASUREMENT_COLUMNS:
                if name not in existing:
                    conn.execute(f"ALTER TABLE measurements ADD COLUMN {name} {decl}")
            # The explicit unique index requested for idempotent upserts.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_measurements_dedup "
                f"ON measurements ({', '.join(_MEASUREMENT_PK)})"
            )
            # Range-query index (ts is the leading range predicate).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_measurements_range "
                "ON measurements (ts, device, channel, ad)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sources ("
                "name TEXT PRIMARY KEY,"
                "rows INTEGER,"
                "first_ts TEXT,"
                "last_ts TEXT,"
                "imported_at TEXT)"
            )
            # S4N-LogPoll: side-table tracking which CMI /LOG data files have
            # already been ingested into ``measurements`` (``source='cmi-log'``).
            # ``name`` is the data-file basename; it keys the idempotent
            # download/decode/upsert and the conditional ``/LOG/clear.log``
            # (last-data-first: clear only when every index file is ingested).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cmi_log_ingested ("
                "name TEXT PRIMARY KEY,"
                "size INTEGER,"
                "sha256 TEXT,"
                "ingested_at TEXT)"
            )
            conn.commit()

    # -- writes ---------------------------------------------------------------
    def upsert(self, rows: list[tuple[str, str, int, str, float, str | None, str]], *,
               source_name: str = "schematic") -> int:
        """Idempotently insert ``(ts, device, channel, ad, value, unit, source)``.

        Rows whose composite key already exist are ignored (``INSERT OR
        IGNORE``), so re-persisting the same sample never duplicates.  In the
        same transaction it also refreshes the ``sources`` bookkeeping row for
        ``source_name``.  Returns the number of rows actually inserted.
        """
        if not rows:
            return 0
        sql = (
            "INSERT OR IGNORE INTO measurements (ts, device, channel, ad, value, unit, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with self._lock:
            conn = self._ensure_conn()
            try:
                cursor = conn.executemany(sql, rows)
                stat = conn.execute(
                    "SELECT COUNT(*) AS n, MIN(ts) AS f, MAX(ts) AS l FROM measurements"
                ).fetchone()
                conn.execute(
                    "INSERT INTO sources (name, rows, first_ts, last_ts, imported_at) "
                    "VALUES (?, ?, ?, ?, datetime('now', 'localtime')) "
                    "ON CONFLICT(name) DO UPDATE SET rows=excluded.rows, "
                    "first_ts=excluded.first_ts, last_ts=excluded.last_ts, "
                    "imported_at=excluded.imported_at",
                    (source_name, int(stat["n"]), stat["f"], stat["l"]),
                )
                conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                conn.rollback()
                raise sqlite3.DatabaseError(str(exc)) from exc

    def retention_cleanup(self, days: float, prune_sources: frozenset[str] | set[str] | None = None) -> int:
        """Delete measurements older than ``days`` -- but ONLY for pruned sources.

        DECISION (human-approved): retention applies ONLY to live-polling
        sources (default ``{"schematic", "json"}``).  Rows whose ``source`` is
        NOT in ``prune_sources`` (e.g. the backfilled ``winsol`` history) are
        EXEMPT and are never deleted, no matter how old their ``ts`` is.  An
        empty ``prune_sources`` set disables pruning entirely.

        Uses naive local datetimes to stay consistent with the stored wall-clock
        ``ts``.  Returns the number of rows deleted.
        """
        prune = set(prune_sources) if prune_sources is not None else set(DEFAULT_PRUNE_SOURCES)
        if not prune:
            return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime(_TS_FMT)
        in_list = ",".join("?" for _ in prune)
        # Exact prune predicate: age window AND source is a live-poll source.
        sql = f"DELETE FROM measurements WHERE ts < ? AND source IN ({in_list})"
        with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(sql, [cutoff, *sorted(prune)])
            conn.commit()
            return int(cursor.rowcount or 0)

    # -- reads ----------------------------------------------------------------
    # -- CMI /LOG ingestion side-table (S4N-LogPoll) -------------------------
    def cmi_log_ingested_meta(self) -> dict[str, int]:
        """``{name: size}`` for every ingested CMI /LOG data file.

        ``size`` is the byte size the /LOG index reported when the file was
        last ingested (written by :meth:`cmi_log_mark` from the index
        snapshot).  The poller uses this for the name+size dedup
        (S4N-LogPoll-DedupFix): an index entry whose size differs from the
        stored one -- in particular the still-growing *active block* file,
        which keeps being appended while it is listed in the index -- is
        re-fetched, and the conditional ``/LOG/clear.log`` gate requires
        every index file to be present at exactly the index size.
        """
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute("SELECT name, size FROM cmi_log_ingested").fetchall()
        # A NULL/0 stored size can only come from pre-dedup-fix rows or manual
        # edits; 0 never matches a real index size, so such a file is safely
        # re-fetched on the next cycle.
        return {row["name"]: int(row["size"] or 0) for row in rows}

    def cmi_log_ingested_names(self) -> set[str]:
        """Set of data-file names already ingested (name-only view).

        Kept for callers that do not care about the stored size (e.g. the
        historical one-shot ``analysis/cmi_log_first_cycle.py`` and tests).
        Size-aware callers (poller dedup, clear gate) use
        :meth:`cmi_log_ingested_meta`.
        """
        return set(self.cmi_log_ingested_meta())

    def cmi_log_get(self, name: str) -> tuple[int, str, str] | None:
        """``(size, sha256, ingested_at)`` for an ingested data file, or ``None``."""
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT size, sha256, ingested_at FROM cmi_log_ingested WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return (int(row["size"]), row["sha256"], row["ingested_at"])

    def cmi_log_mark(self, name: str, size: int, sha256: str, ingested_at: str) -> None:
        """Record that ``name`` (a data-file basename) has been ingested.

        ``INSERT OR REPLACE`` upsert on ``PRIMARY KEY (name)``: re-ingesting a
        file -- e.g. a still-growing active block re-fetched by the
        name+size dedup (S4N-LogPoll-DedupFix) -- OVERWRITES the stored
        ``size``/``sha256``/``ingested_at`` with the fresh values (the index
        size the download was verified against), so the next cycle dedups on
        the new size.
        """
        with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "INSERT OR REPLACE INTO cmi_log_ingested "
                "(name, size, sha256, ingested_at) VALUES (?, ?, ?, ?)",
                (name, int(size), sha256, ingested_at),
            )
            conn.commit()

    def cmi_log_cleanup(self) -> int:
        """Delete the pre-S4N-LogParquet cmi-log rows from ``measurements``.

        S4N-LogPoll persisted the CMI /LOG data as long rows
        (``source='cmi-log'``); S4N-LogParquet moved that history into the
        cmi-log Parquet store, so the long rows must be removed once.  The
        ``cmi_log_ingested`` side table is deliberately NOT touched: it still
        keys the poller's download dedup and the clear gate.  Idempotent:
        returns the number of rows actually deleted (0 when already clean).
        """
        with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute("DELETE FROM measurements WHERE source = 'cmi-log'")
            conn.commit()
            return int(cursor.rowcount or 0)

    def stats(self) -> dict:
        """Row count + first/last timestamp of ``measurements``."""
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
                " FROM measurements"
            ).fetchone()
            return {"rows": int(row["n"]), "first_ts": row["first_ts"], "last_ts": row["last_ts"]}

    def query_series(self, q: HistoryQuery) -> dict:
        """Run a history query and shape the JSON response.

        Returns either a *single* series object (one device/channel) or a dict
        keyed by ``"device:channel"`` when more than one combination is
        present.  See :meth:`_points_for` for the aggregation/interval rules.
        """
        m = self.query_series_map(q)
        if not m:
            return {}
        if len(m) == 1:
            key, points = next(iter(m.items()))
            device, channel = key.rsplit(":", 1)
            return {
                "device": device,
                "channel": int(channel),
                "points": points,
                "count": len(points),
                "agg": q.agg,
                "interval": q.interval,
            }
        out: dict[str, dict] = {}
        total = 0
        for key, points in m.items():
            out[key] = {
                "points": points,
                "count": len(points),
                "agg": q.agg,
                "interval": q.interval,
            }
            total += len(points)
        out.setdefault("_meta", {"count": total, "agg": q.agg, "interval": q.interval})
        return out

    def query_series_map(self, q: HistoryQuery) -> dict[str, list[dict]]:
        """Return ``{f"{device}:{channel}": points}`` for every matching combo.

        This is the shape-agnostic core of :meth:`query_series`: it performs the
        same aggregation/interval/limit rules but does NOT decide between the
        flat (single series) and the dict (multi series) response shapes.  The
        ``/api/v1/history`` route uses it to *merge* long-store and WinSol
        wide-store series into one consistent response.
        """
        if q.agg not in _ALLOWED_AGGS:
            raise ValueError(f"unsupported agg {q.agg!r} (want one of {sorted(_ALLOWED_AGGS)})")
        q.limit = max(1, min(q.limit, MAX_HISTORY_LIMIT))
        out: dict[str, list[dict]] = {}
        for device, channel in self._distinct_combos(q):
            out[f"{device}:{channel}"] = self._points_for(device, channel, q)
        return out

    # -- query helpers --------------------------------------------------------
    def _where(self, q: HistoryQuery, device: str | None = None,
               channel: int | None = None) -> tuple[str, list]:
        # Per-combo overrides win; otherwise fall back to the query-level
        # filters so combo discovery and point retrieval stay consistent.
        clauses = ["ts >= ?", "ts <= ?"]
        params: list = [q.start, q.end]
        use_device = device if device is not None else q.device
        if use_device is not None:
            clauses.append("device = ?")
            params.append(use_device)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        elif q.channels:
            clauses.append(f"channel IN ({','.join('?' * len(q.channels))})")
            params.extend(q.channels)
        if q.metric:
            clauses.append("source = ?")
            params.append(q.metric)
        if q.ad:
            clauses.append("ad = ?")
            params.append(q.ad)
        return " AND ".join(clauses), params

    def _distinct_combos(self, q: HistoryQuery) -> list[tuple[str, int]]:
        where, params = self._where(q)
        sql = f"SELECT DISTINCT device, channel FROM measurements WHERE {where}"
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(sql, params).fetchall()
        return [(r["device"], int(r["channel"])) for r in rows]

    def _points_for(self, device: str, channel: int, q: HistoryQuery) -> list[dict]:
        where, params = self._where(q, device=device, channel=channel)
        with self._lock:
            conn = self._ensure_conn()
            if q.interval and q.agg in ("min", "max", "avg"):
                return self._bucketed(conn, q.agg, where, params, q)
            if q.interval and q.agg == "last":
                return self._last_per_bucket(conn, where, params, device, channel, q)
            if q.agg in ("min", "max", "avg"):
                return self._single_aggregate(conn, q.agg, where, params)
            # agg == 'last', no interval: the most recent N samples.
            sql = (
                f"SELECT ts, value FROM measurements WHERE {where} "
                "AND value IS NOT NULL ORDER BY ts DESC LIMIT ?"
            )
            rows = conn.execute(sql, [*params, q.limit]).fetchall()
        points = [{"ts": r["ts"], "value": r["value"]} for r in reversed(rows)]
        return points

    def _bucketed(self, conn, agg: str, where: str, params: list, q: HistoryQuery) -> list[dict]:
        sql = (
            "SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS b, "
            f"{agg}(value) AS v FROM measurements WHERE {where} "
            "AND value IS NOT NULL GROUP BY b ORDER BY b DESC LIMIT ?"
        )
        rows = conn.execute(sql, [q.interval, q.interval, *params, q.limit]).fetchall()
        points = [
            {"ts": _bucket_to_ts(r["b"]), "value": _round(r["v"])} for r in reversed(rows)
        ]
        return points

    def _last_per_bucket(
        self, conn, where: str, params: list, device: str, channel: int, q: HistoryQuery
    ) -> list[dict]:
        # Value of the most-recent sample within each bucket.  ``where``/
        # ``params`` are combo-scoped (device + channel pinned), so the inner
        # ``MAX(ts)`` marker can never point at a foreign combo's sample:
        # the outer join ``m.ts = x.mt`` + combo clauses then picks exactly
        # this combo's row per bucket.
        inner = (
            "SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS b, MAX(ts) AS mt "
            f"FROM measurements WHERE {where} AND value IS NOT NULL GROUP BY b"
        )
        sql = (
            "SELECT m.ts, m.value FROM measurements m "
            f"JOIN ({inner}) x ON m.ts = x.mt "
            f"WHERE {where} AND m.device = ? AND m.channel = ? AND m.value IS NOT NULL "
            "ORDER BY m.ts DESC LIMIT ?"
        )
        # Bindings: interval x2 + where-params (inner) + where-params (outer)
        # + the combo device/channel + limit -- exactly one parameter per
        # ``?`` placeholder (the S4N-2-Fix: the outer device/channel pair
        # was previously never bound).
        rows = conn.execute(
            sql, [q.interval, q.interval, *params, *params, device, channel, q.limit]
        ).fetchall()
        return [{"ts": r["ts"], "value": _round(r["value"])} for r in reversed(rows)]

    def _single_aggregate(self, conn, agg: str, where: str, params: list) -> list[dict]:
        sql = (
            f"SELECT {agg}(value) AS v, MAX(ts) AS t FROM measurements "
            f"WHERE {where} AND value IS NOT NULL"
        )
        row = conn.execute(sql, params).fetchone()
        if row is None or row["v"] is None:
            return []
        return [{"ts": row["t"], "value": _round(row["v"])}]


# ---------------------------------------------------------------------------
# module-level helpers / factory
# ---------------------------------------------------------------------------
def _bucket_to_ts(unix_bucket: int) -> str:
    """Convert a unix-epoch bucket start back to the naive wall-clock format.

    ``strftime('%s', ts)`` in SQLite interprets the naive ``ts`` as UTC, so the
    inverse uses UTC too -- the round-trip keeps bucket labels consistent with
    the stored timestamps regardless of local timezone.
    """
    return datetime.fromtimestamp(unix_bucket, tz=UTC).strftime(_TS_FMT)


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


_db_instances: dict[str, HistoryDB] = {}
_instances_lock = threading.Lock()


def get_history_db(path: str | Path, *, busy_timeout_ms: int = 5_000) -> HistoryDB:
    """Return a process-wide :class:`HistoryDB` for ``path`` (created on demand).

    The first caller must invoke :meth:`HistoryDB.init_schema` (``create_app``
    does this at startup).  Keyed by absolute path so different test DBs do not
    share a connection.
    """
    key = str(Path(path).resolve())
    with _instances_lock:
        db = _db_instances.get(key)
        if db is None:
            db = HistoryDB(path, busy_timeout_ms=busy_timeout_ms)
            _db_instances[key] = db
        return db


def reset_db_cache() -> None:
    """Close and drop all cached :class:`HistoryDB` instances (tests / shutdown)."""
    global _db_instances
    with _instances_lock:
        for db in _db_instances.values():
            db.close()
        _db_instances = {}
