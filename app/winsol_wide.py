"""On-demand WinSol history served from the canonical *Parquet* store.

ARCHITECTURE (human-approved, S2-Parquet-Pivot): Parquet is the SINGLE
canonical form for the WinSol history.  The gateway serves it ON DEMAND for the
``winsol:<N>`` devices of ``GET /api/v1/history`` instead of copying it into the
long store (``data/history.db``), which holds only live-polling
``schematic`` / ``json:<n>`` rows.

:class:`WinsolParquet` opens the Hive-partitioned Parquet tree
(``data/winsol_pq/YYYY/MM/YYYY-MM.parquet``, one zstd file per month) via
DuckDB's native Parquet reader and, for a given ``winsol:<N>`` device + channel
+ A/D flag, unpivots the matching wide column(s) into the same
``[{"ts","value"}]`` point shape the long store's ``/api/v1/history`` already
returns, so the UI sees one contract.

Wide layout (77 -> 71 columns; meta columns dropped, derivable from ``ts``):
``ts`` (canonical ``'YYYY-MM-DD HH:MM:SS'`` STRING -- kept naive local, NOT
converted to a timestamp/timezone) + the 70 value columns
``dev{0,1,2}_analog_{01..16}`` and ``dev{0,1,2}_digital_{01..13}``.

Design notes:

* ``ts`` is a STRING in the canonical zero-padded format, so lexicographic
  ``ts >= ? AND ts <= ?`` is a correct range predicate.  NOTE: the bare
  ``YYYY/MM`` directory names do NOT create hive columns in the Parquet schema,
  so DuckDB opens ALL month files per query; what makes the range fast is
  ROW-GROUP pruning driven by the ``ts`` filter (measured ~3 MB read / ~30 ms
  per query on the full store, results always correct).  True hive-column
  partition pruning would require the partition columns in the Parquet schema
  itself.
* ``ad=None`` unions BOTH the analog and digital wide columns for a channel
  (mirrors the long store, which does not filter on ``ad``); ``ad='A'``/``'D'``
  takes one.
* Read-only.  One shared in-memory DuckDB connection is guarded by a lock
  (DuckDB is not thread-safe across concurrent use); the route drives it via
  ``asyncio.to_thread``.  A missing/empty Parquet dir degrades cleanly (store
  error, or simply no winsol series) and never crashes gateway startup.
* ``available_channels`` / column resolution are cheap dict lookups backed by a
  schema-derived column map, so they do not touch the store on the hot path.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import duckdb

logger = logging.getLogger("cmi.winsol_wide")

#: Device tag the gateway namespaces WinSol history under: ``winsol:<N>``.
_WIDE_DEVICE_RE = re.compile(r"^winsol:(\d+)$")
#: Public alias so the API layer does not duplicate the pattern.
WINSOL_DEVICE_RE = _WIDE_DEVICE_RE
#: Wide value column name: ``dev{N}_{analog|digital}_{MM}`` (MM zero-padded).
_WIDE_COL_RE = re.compile(r"^dev(\d+)_(analog|digital)_(\d+)$")

_TS_FMT = "%Y-%m-%d %H:%M:%S"
_VALID_AGGS = frozenset({"last", "min", "max", "avg"})
#: Upper bound for a single wide-store series (matches the long store's cap).
MAX_WINSOL_LIMIT = 100_000
DEFAULT_WINSOL_LIMIT = 5_000
#: The three WinSol device indices (the wide schema only has dev0/dev1/dev2).
_DEVICE_INDICES = (0, 1, 2)


class WinsolWideError(Exception):
    """Base class for on-demand WinSol store errors."""


class WinsolInputError(WinsolWideError):
    """Bad user input: unknown device tag, bad A/D flag, or channel out of the
    device's range (maps to HTTP 400)."""


class WinsolStoreError(WinsolWideError):
    """Store-level problem: Parquet dir missing/empty or a query failure
    (maps to HTTP 500)."""


def _round(value: float | None) -> float | None:
    """Round a sample to 4 decimals (mirrors the long store's rounding)."""
    if value is None:
        return None
    return round(float(value), 4)


def _bucket_ts(unix_bucket: int) -> str:
    """Bucket-epoch -> naive wall-clock label.

    Mirrors the long store: the bucket epoch is computed as the Unix epoch of the
    (naive, UTC-interpreted) timestamp -- identical to SQLite's
    ``strftime('%s', ts)`` -- so the inverse uses UTC too, keeping bucket labels
    consistent with the stored timestamps regardless of local timezone.
    """
    return datetime.fromtimestamp(unix_bucket, tz=UTC).strftime(_TS_FMT)


class WinsolParquet:
    """Read-only on-demand accessor for the WinSol Parquet history store.

    A single lazily-opened in-memory DuckDB connection is guarded by a lock and
    driven from the worker threads that FastAPI spawns for
    ``/api/v1/history``.  The Parquet tree is opened read-only; nothing is ever
    written.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._glob = f"{self._path}/*/*/*.parquet"
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._colmap: dict[tuple[int, str, int], str] | None = None
        self._available = False
        self._months: list[str] = []
        self._filesystem_probe()

    # -- filesystem probe (cheap, for /health; no store scan) -----------------
    def _filesystem_probe(self) -> None:
        months: set[str] = set()
        try:
            if self._path.is_dir():
                for year_dir in self._path.iterdir():
                    if not year_dir.is_dir() or not year_dir.name.isdigit():
                        continue
                    for month_dir in year_dir.iterdir():
                        if month_dir.is_dir() and re.fullmatch(r"\d{2}", month_dir.name):
                            months.add(f"{year_dir.name}-{month_dir.name}")
        except OSError:  # pragma: no cover - permission/IO oddities
            months = set()
        self._months = sorted(months)
        self._available = bool(self._months)
        if not self._available:
            logger.warning("winsol parquet store not found/empty: %s", self._path)

    def is_available(self) -> bool:
        """True when the Parquet dir exists and holds at least one month."""
        return self._available

    def months_range(self) -> list[str] | None:
        """``[first_month, last_month]`` (``YYYY-MM``) or ``None`` when empty.

        Derived from the filesystem layout (``YYYY/MM`` dirs), NOT a store scan.
        """
        if not self._months:
            return None
        return [self._months[0], self._months[-1]]

    # -- connection lifecycle -------------------------------------------------
    def _ensure_conn(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self._con = duckdb.connect()  # in-memory; parquet read from disk
        return self._con

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None
            self._colmap = None

    # -- column mapping -------------------------------------------------------
    def _colmap_cached(self) -> dict[tuple[int, str, int], str]:
        """Map ``(device_index, ad, channel) -> wide column name``.

        Built ONCE from the actual Parquet schema (``DESCRIBE``), so the mapping
        matches the real zero-padded column names.  Cheap afterwards.
        """
        if self._colmap is not None:
            return self._colmap
        with self._lock:
            if self._colmap is not None:
                return self._colmap
            colmap: dict[tuple[int, str, int], str] = {}
            try:
                rows = self._ensure_conn().execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{self._glob}', hive_partitioning=true)"
                ).fetchall()
                for row in rows:
                    name = str(row[0])
                    m = _WIDE_COL_RE.match(name)
                    if not m:
                        continue
                    dev = int(m.group(1))
                    ad = "A" if m.group(2) == "analog" else "D"
                    channel = int(m.group(3))
                    colmap[(dev, ad, channel)] = name
            except duckdb.Error as exc:  # empty/missing dir -> no columns
                logger.warning("winsol colmap: could not read schema: %s", exc)
            self._colmap = colmap
            return colmap

    @staticmethod
    def _parse_device(device: str) -> int:
        m = _WIDE_DEVICE_RE.match(device or "")
        if not m:
            raise WinsolInputError(f"device must be 'winsol:<N>', got {device!r}")
        dev = int(m.group(1))
        if dev not in _DEVICE_INDICES:
            raise WinsolInputError(
                f"device index must be 0..2, got {dev!r} (have {device!r})"
            )
        return dev

    @staticmethod
    def _parse_ad(ad: str | None) -> list[str]:
        """Normalise the A/D filter to the list of A/D types to include.

        ``None`` -> both ``['A','D']`` (mirror the long store's no-ad-filter
        behaviour); ``'A'``/``'D'`` -> that single type; anything else is a
        :class:`WinsolInputError`.
        """
        if ad is None:
            return ["A", "D"]
        if ad in ("A", "D"):
            return [ad]
        raise WinsolInputError(f"ad must be 'A', 'D' or None, got {ad!r}")

    def available_channels(self, device: str, ad: str | None = None) -> set[int]:
        """The set of channels that exist for a ``winsol:<N>`` device (union
        over the requested A/D types).  Returns an empty set for an unknown
        device or when the store is unavailable."""
        dev = self._parse_device(device)
        ads = self._parse_ad(ad)
        colmap = self._colmap_cached()
        return {ch for (d, a, ch) in colmap if d == dev and a in ads}

    def resolve_columns(self, device: str, channel: int, ad: str | None = None) -> list[str]:
        """Resolve a ``winsol:<N>``/channel request to the wide column name(s)
        to read (one per requested A/D type that actually has that channel).

        Raises :class:`WinsolInputError` for an unknown device tag, a bad A/D
        flag, or when *none* of the requested A/D types has the channel
        (the error lists the available channels for that device).
        """
        dev = self._parse_device(device)
        ads = self._parse_ad(ad)
        colmap = self._colmap_cached()
        if not colmap:
            # No wide columns at all -> the store is missing/empty (a store-level
            # problem, not a user-input one): map to 500, not 400.
            raise WinsolStoreError(f"WinSol parquet store unavailable: {self._path}")
        cols: list[str] = []
        for a in ads:
            key = (dev, a, int(channel))
            if key in colmap:
                cols.append(colmap[key])
        if not cols:
            have = sorted(ch for (d, a, ch) in colmap if d == dev and a in ads)
            raise WinsolInputError(
                f"channel {channel} not available for {device} "
                f"ad={ad or 'A,D'} (available channels: {have})"
            )
        return cols

    # -- queries --------------------------------------------------------------
    def query(
        self,
        device: str,
        channel: int,
        ad: str | None,
        start: str,
        end: str,
        *,
        agg: str = "last",
        interval: int | None = None,
        limit: int = DEFAULT_WINSOL_LIMIT,
    ) -> list[dict]:
        """Return ``[{"ts","value"}]`` for one wide series, matching the long
        store's point shape and aggregation/interval semantics.

        ``start``/``end`` are canonical ``YYYY-MM-DD HH:MM:SS`` strings (the
        route normalises user input via :func:`app.db.parse_ts`).  ``ad`` may be
        ``None`` to union both A and D columns for the channel.
        """
        if agg not in _VALID_AGGS:
            raise WinsolInputError(f"invalid agg {agg!r}; expected one of {sorted(_VALID_AGGS)}")
        cols = self.resolve_columns(device, channel, ad)
        limit = max(1, min(int(limit), MAX_WINSOL_LIMIT))
        where = "ts >= ? AND ts <= ?"
        params: list = [start, end]
        base = self._value_cte(cols, where)
        with self._lock:
            con = self._ensure_conn()
            try:
                if interval and agg in ("min", "max", "avg"):
                    return self._bucketed(con, base, params, agg, int(interval), limit)
                if interval and agg == "last":
                    return self._last_per_bucket(con, base, params, int(interval), limit)
                if agg in ("min", "max", "avg"):
                    return self._single_agg(con, base, params, agg)
                # agg == 'last', no interval: the most recent N samples.
                rows = con.execute(
                    f"{base} SELECT ts, value FROM base ORDER BY ts DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                return [{"ts": r[0], "value": _round(r[1])} for r in reversed(rows)]
            except duckdb.Error as exc:
                raise WinsolStoreError(f"WinSol parquet query failed: {exc}") from exc

    def _value_cte(self, cols: list[str], where: str) -> str:
        """Build the ``WITH`` prefix exposing an unpivoted ``(ts, value)`` base.

        The range filter (``where``) binds exactly ONCE in a ``src`` CTE (which
        also prunes month partitions); the per-column NULL filters reference
        ``src`` and add no range placeholders, so any number of wide columns
        maps to the same single ``[start, end]`` binding.
        """
        src = (
            f"src AS (SELECT ts, {', '.join(cols)} "
            f"FROM read_parquet('{self._glob}', hive_partitioning=true) WHERE {where})"
        )
        union = " UNION ALL ".join(
            f"SELECT ts, {c} AS value FROM src WHERE {c} IS NOT NULL" for c in cols
        )
        return f"WITH {src}, base AS ({union}) "

    def _bucketed(
        self, con, base: str, params: list, agg: str, interval: int, limit: int
    ) -> list[dict]:
        sql = (
            f"{base}"
            "SELECT (CAST(epoch(CAST(ts AS TIMESTAMP)) AS INTEGER) // ?) * ? AS b, "
            f"{agg}(value) AS v FROM base GROUP BY b ORDER BY b DESC LIMIT ?"
        )
        rows = con.execute(sql, [*params, interval, interval, limit]).fetchall()
        return [{"ts": _bucket_ts(r[0]), "value": _round(r[1])} for r in reversed(rows)]

    def _last_per_bucket(
        self, con, base: str, params: list, interval: int, limit: int
    ) -> list[dict]:
        # Value of the most-recent sample within each bucket.
        sql = (
            f"{base}"
            "SELECT m.ts, m.value FROM base AS m "
            "JOIN (SELECT (CAST(epoch(CAST(ts AS TIMESTAMP)) AS INTEGER) // ?) * ? AS b, "
            "MAX(ts) AS mt FROM base GROUP BY b) AS bk ON m.ts = bk.mt "
            "ORDER BY m.ts DESC LIMIT ?"
        )
        rows = con.execute(sql, [*params, interval, interval, limit]).fetchall()
        return [{"ts": r[0], "value": _round(r[1])} for r in reversed(rows)]

    def _single_agg(self, con, base: str, params: list, agg: str) -> list[dict]:
        sql = f"{base}SELECT {agg}(value) AS v, MAX(ts) AS t FROM base"
        row = con.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return []
        return [{"ts": row[1], "value": _round(row[0])}]
