"""CMI internal-logging (/LOG) history in the *Parquet* wide store (S4N-LogParquet).

Decision Option 1 (HITL maintainer, 2026-10-30): the CMI's internal logging is a
*second* history source that feeds the SAME logical series as the WinSol
archive, but keeps its own canonical Parquet tree::

    <dir>/YYYY/MM/YYYY-MM.parquet     (zstd, Hive layout like winsol_pq)

with the canonical **74-column** wide schema: the 71 ``winsol_pq`` columns
(``ts`` STRING ``'YYYY-MM-DD HH:MM:SS'`` naive local + 70 value columns
``dev{0,1,2}_analog_{01..16}`` / ``dev{0,1,2}_digital_{02..09, 01..13,
01}``, all double) PLUS the three Q-125 channels
``dev2_analog_17/18/19`` (Sollspannung 1/2/3, the new P=181 Minderer-
Sollwerte introduced 2026-11-07 16:14, see
:mod:`app.cmi.cmi_log` + docs/cmi-log-protocol.md Kap. 3.1).  Since
S4N-LogFull (Q-112b, 2026-11-01) the P=169 decoder emits all 70 value
channels per record; the P=181 decoder (Q-125) emits 73 value channels per
record (the three new dev2 analog channels).  P=169 records carry no
dev2 a17-a19 payload, so those three columns read as NULL for P=169 data;
P=181 records populate them.  The only other fills not read from a CMI
payload byte are the documented constants (dev0 a11-a15 = 0.0, dev1 a10 =
0.0 with the -1.0 LogX-canon divergence, dev1 a14-a16 = 0.0, dev1
d1-d13 = 0.0, dev2 a10 = 0.0 on the recorded days).

WRITE SEMANTICS -- month rewrite (live store):
  * Only the CURRENT month's file is ever rewritten (the poller runs every 5
    min, so the in-progress day file is continuously appended).  Finished
    months are FROZEN and never touched -- they are byte-stable after the
    last rewrite of their month.
  * A rewrite = read the existing month file (if any) -> merge the new
    records, de-duplicated on ``ts`` (existing rows win; a ts that is already
    present in the store is never overwritten) -> write the union to a
    ``.part`` temp file in the same directory -> atomic ``os.replace`` onto
    ``YYYY-MM.parquet``.  Readers therefore always see either the old or the
    new complete file, never a partial one.  No DuckDB on the write path
    (pyarrow only), so the 5-min rewrite cannot contend with the query
    connection.

READ SEMANTICS -- mirrors :class:`app.winsol_wide.WinsolParquet`:
  * One shared in-memory DuckDB connection guarded by a lock, driven via
    ``asyncio.to_thread`` from the history route.
  * ``device`` is ALWAYS the logical ``winsol:<N>`` tag (the route merges
    cmi_log_pq into the winsol series; a ``cmi-log:<N>`` tag is rejected).
  * A missing/empty dir degrades cleanly (no cmi-log series, no store
    error) and never crashes gateway startup.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from app.winsol_wide import _bucket_ts, _round

logger = logging.getLogger("cmi.cmi_log_pq")

#: Hive layout inside the store directory.
_MONTH_FILE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}(-\d{2})\.parquet$")
#: Device tag: this store serves the LOGICAL winsol series only.
_DEVICE_RE: Final[re.Pattern[str]] = re.compile(r"^winsol:(\d+)$")
#: Upper bound for a single wide-store series (mirrors the winsol store).
MAX_LIMIT: Final[int] = 100_000
DEFAULT_LIMIT: Final[int] = 5_000

#: The canonical 74-column wide schema: the 71 ``winsol_pq`` columns PLUS the
#: three Q-125 channels ``dev2_analog_17/18/19`` (Sollspannung 1/2/3, the new
#: P=181 Minderer-Sollwerte introduced 2026-11-07 16:14).  ``ts`` is STRING
#: + 73 double value columns, in schema order: per device the analog block
#: followed by the digital block (exactly as written by
#: ``analysis/winsol_to_parquet.py`` for the 71 canonical columns, with the
#: three new dev2 analog columns appended after ``dev2_analog_16``).  Every
#: month file of this store is written against this exact 74-column schema;
#: any OTHER schema is a corruption and is treated as a store error on read
#: -- with the single documented exception of the legacy 71-column P=169
#: month files, which are migrated on the write path (see
#: :meth:`CmiLogParquet.write_month`) and tolerated on the read path
#: (see :meth:`CmiLogParquet._colcounts`).
_DIGITAL_CHANNELS: Final[dict[int, tuple[int, ...]]] = {
    0: tuple(range(2, 10)),   # dev0 digital d2-d9 (per Setup.xml)
    1: tuple(range(1, 14)),   # dev1 digital d1-d13
    2: (1,),                  # dev2 digital d1 (Solarpumpe)
}
_ANALOG_CHANNELS: Final[dict[int, tuple[int, ...]]] = {
    0: tuple(range(1, 17)),   # dev0 analog a1-a16
    1: tuple(range(1, 17)),   # dev1 analog a1-a16
    2: tuple(range(1, 20)),   # dev2 analog a1-a19 (Q-125: + a17/a18/a19)
}
WIDE_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    *[
        f"dev{d}_{kind}_{c:02d}"
        for d in (0, 1, 2)
        for kind, chans in (
            ("analog", _ANALOG_CHANNELS[d]),
            ("digital", _DIGITAL_CHANNELS[d]),
        )
        for c in chans
    ],
)
_WIDE_SCHEMA: Final[pa.Schema] = pa.schema([("ts", pa.string()), *( (c, pa.float64()) for c in WIDE_COLUMNS[1:] )])

#: The three Q-125 value columns added to the legacy 71-column P=169 schema
#: (in :data:`WIDE_COLUMNS` order, i.e. after ``dev2_analog_16``).  A month
#: file that is missing EXACTLY these three columns is the legacy P=169
#: schema and is migrated to the canonical 74-column schema; anything else is
#: a corruption.
_NEW_DEV2_ANALOG_COLUMNS: Final[tuple[str, ...]] = (
    "dev2_analog_17",
    "dev2_analog_18",
    "dev2_analog_19",
)


class CmiLogPqError(Exception):
    """Base class for cmi_log_pq store errors."""


class CmiLogPqInputError(CmiLogPqError):
    """Bad user input: unknown device tag or channel out of range (HTTP 400)."""


class CmiLogPqStoreError(CmiLogPqError):
    """Store-level problem: unreadable/corrupt month file or query failure (HTTP 500)."""


class CmiLogParquet:
    """Wide Parquet store for the CMI internal logging (read + live-month write).

    The read half mirrors :class:`app.winsol_wide.WinsolParquet` (same DuckDB
    approach: shared in-memory connection, ``read_parquet`` glob, ts-string
    range pruning, ``[{"ts","value"}]`` point shape).  The write half is the
    live-month rewrite described in the module doc.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._glob = f"{self._path}/*/*/*.parquet"
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._colmap: dict[tuple[int, str, int], str] | None = None
        self._colcount_cache: dict[tuple[int, str, int], int] | None = None
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

    def _refresh_probe(self) -> None:
        """Re-run the cheap filesystem probe + invalidate the column counts
        (a write may add a month and new non-NULL channels)."""
        self._filesystem_probe()
        self._colcount_cache = None

    def is_available(self) -> bool:
        """True when the store dir exists and holds at least one month file."""
        return bool(self._months)

    def months_range(self) -> list[str] | None:
        """Distinct stored months, ``YYYY-MM``, sorted ascending; ``None`` when
        the store is empty.  A single month yields ``["YYYY-MM"]`` (not a
        ``[first, last]`` pair with itself)."""
        if not self._months:
            return None
        return sorted(set(self._months))

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
            self._colcount_cache = None

    # -- column mapping -------------------------------------------------------
    def _colmap_cached(self) -> dict[tuple[int, str, int], str]:
        """Map ``(device_index, ad, channel) -> wide column name`` (schema order).

        The 71-column schema is fixed, so the colmap is a pure function of
        :data:`WIDE_COLUMNS`; it is built once.
        """
        if self._colmap is not None:
            return self._colmap
        colmap: dict[tuple[int, str, int], str] = {}
        for name in WIDE_COLUMNS[1:]:
            m = re.match(r"^dev(\d+)_(analog|digital)_(\d+)$", name)
            if m:
                colmap[(int(m.group(1)), "A" if m.group(2) == "analog" else "D", int(m.group(3)))] = name
        self._colmap = colmap
        return colmap

    def _colcounts(self) -> dict[tuple[int, str, int], int]:
        """Non-NULL sample counts per ``(dev, ad, channel)`` across all months.

        Built ONCE with a single store scan (one query, all 70 aggregates);
        invalidated after every write, because a new month can introduce
        channels that were NULL before (the cmi-log coverage grows with the
        data).  An unreadable/empty store yields an empty mapping (the route
        then simply requests no cmi-log series).
        """
        if self._colcount_cache is not None:
            return self._colcount_cache
        with self._lock:
            if self._colcount_cache is not None:
                return self._colcount_cache
            counts: dict[tuple[int, str, int], int] = {}
            try:
                if self._months:
                    con = self._ensure_conn()
                    names = [n for n in WIDE_COLUMNS[1:]]
                    sql = (
                        "SELECT "
                        + ", ".join(f"COUNT({n})" for n in names)
                        + f" FROM read_parquet('{self._glob}', hive_partitioning=true)"
                    )
                    row = con.execute(sql).fetchone()
                    for i, name in enumerate(names):
                        m = re.match(r"^dev(\d+)_(analog|digital)_(\d+)$", name)
                        key = (
                            int(m.group(1)),
                            "A" if m.group(2) == "analog" else "D",
                            int(m.group(3)),
                        )
                        counts[key] = int(row[i])
            except duckdb.Error as exc:
                logger.warning("cmi-log pq: column counts unavailable: %s", exc)
            self._colcount_cache = counts
            return counts

    @staticmethod
    def _parse_device(device: str) -> int:
        m = _DEVICE_RE.match(device or "")
        if not m:
            raise CmiLogPqInputError(
                f"cmi-log store serves the logical 'winsol:<N>' tag only, got {device!r}"
            )
        dev = int(m.group(1))
        if dev > 2:
            raise CmiLogPqInputError(f"device index must be 0..2, got {dev!r}")
        return dev

    @staticmethod
    def _parse_ad(ad: str | None) -> list[str]:
        if ad is None:
            return ["A", "D"]
        if ad in ("A", "D"):
            return [ad]
        raise CmiLogPqInputError(f"ad must be 'A', 'D' or None, got {ad!r}")

    def available_channels(self, device: str, ad: str | None = None) -> set[int]:
        """Channels that exist for a ``winsol:<N>`` device in this store.

        Data-driven: a channel "exists" when at least one stored sample is
        non-NULL in its wide column (the cmi-log coverage grows with the
        ingested data; NULL-only columns are absent).  Empty when the store is
        empty (nothing ingested yet) or for an unknown device; the route
        treats that as "no cmi-log series" without failing.
        """
        dev = self._parse_device(device)
        ads = self._parse_ad(ad)
        if not self._months:
            return set()
        counts = self._colcounts()
        return {ch for (d, a, ch), n in counts.items() if d == dev and a in ads and n > 0}

    def resolve_columns(self, device: str, channel: int, ad: str | None = None) -> list[str]:
        """Resolve a request to the wide column name(s) to read.

        Raises :class:`CmiLogPqInputError` for an unknown device tag, a bad
        A/D flag, or a channel the device does not carry in this store.  Raises
        :class:`CmiLogPqStoreError` when the store is empty (nothing ingested
        yet -- a store-level, not user-input, problem).
        """
        dev = self._parse_device(device)
        ads = self._parse_ad(ad)
        if not self._months:
            raise CmiLogPqStoreError(f"cmi-log parquet store unavailable: {self._path}")
        counts = self._colcounts()
        colmap = self._colmap_cached()
        cols: list[str] = []
        for a in ads:
            key = (dev, a, int(channel))
            if colmap.get(key) and counts.get(key, 0) > 0:
                cols.append(colmap[key])
        if not cols:
            have = sorted(ch for (d, a, ch), n in counts.items() if d == dev and a in ads and n > 0)
            raise CmiLogPqInputError(
                f"channel {channel} not available for {device} "
                f"ad={ad or 'A,D'} (available channels: {have})"
            )
        return cols

    # -- queries (mirror of WinsolParquet.query) --------------------------------
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
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        """Return ``[{"ts","value"}]`` for one wide series.

        Same point shape and aggregation/interval semantics as
        :meth:`app.winsol_wide.WinsolParquet.query`.  ``start``/``end`` are
        canonical ``YYYY-MM-DD HH:MM:SS`` strings; ``ad=None`` unions both the
        analog and digital columns of the channel.
        """
        if agg not in frozenset({"last", "min", "max", "avg"}):
            raise CmiLogPqInputError(f"invalid agg {agg!r}")
        cols = self.resolve_columns(device, channel, ad)
        limit = max(1, min(int(limit), MAX_LIMIT))
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
                rows = con.execute(
                    f"{base} SELECT ts, value FROM base ORDER BY ts DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                return [{"ts": r[0], "value": _round(r[1])} for r in reversed(rows)]
            except duckdb.Error as exc:
                raise CmiLogPqStoreError(f"cmi-log parquet query failed: {exc}") from exc

    def _value_cte(self, cols: list[str], where: str) -> str:
        """``WITH`` prefix exposing an unpivoted ``(ts, value)`` base (like the
        winsol store: the range filter binds ONCE in the ``src`` CTE)."""
        src = (
            f"src AS (SELECT ts, {', '.join(cols)} "
            f"FROM read_parquet('{self._glob}', hive_partitioning=true) WHERE {where})"
        )
        union = " UNION ALL ".join(
            f"SELECT ts, {c} AS value FROM src WHERE {c} IS NOT NULL" for c in cols
        )
        return f"WITH {src}, base AS ({union}) "

    def _bucketed(self, con, base: str, params: list, agg: str, interval: int, limit: int) -> list[dict]:
        sql = (
            f"{base}"
            "SELECT (CAST(epoch(CAST(ts AS TIMESTAMP)) AS INTEGER) // ?) * ? AS b, "
            f"{agg}(value) AS v FROM base GROUP BY b ORDER BY b DESC LIMIT ?"
        )
        rows = con.execute(sql, [*params, interval, interval, limit]).fetchall()
        return [{"ts": _bucket_ts(r[0]), "value": _round(r[1])} for r in reversed(rows)]

    def _last_per_bucket(self, con, base: str, params: list, interval: int, limit: int) -> list[dict]:
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

    # -- write path: live-month rewrite ---------------------------------------
    def _month_path(self, month: str) -> Path:
        return self._path / month[:4] / month[5:7] / f"{month}.parquet"

    @staticmethod
    def _check_schema(table: pa.Table) -> None:
        """Enforce the canonical 74-column schema (exact names + order + types)."""
        names = table.schema.names
        if names != list(WIDE_COLUMNS):
            raise CmiLogPqStoreError(
                f"month file does not carry the canonical 74-column schema "
                f"({len(names)} columns, expected {len(WIDE_COLUMNS)}; "
                f"first: {names[:5]})"
            )

    @staticmethod
    def _check_legacy_schema(table: pa.Table) -> None:
        """Enforce the legacy P=169 71-column schema (Q-125 migration target).

        A file carrying the 74 canonical columns MINUS the three new
        ``dev2_analog_17/18/19`` columns (exact names + order + types) is the
        pre-Q-125 P=169 store layout; it is readable and is migrated by
        :meth:`write_month` (new columns = NULL).  Anything else is a store
        error.
        """
        expected = [c for c in WIDE_COLUMNS if c not in _NEW_DEV2_ANALOG_COLUMNS]
        if table.schema.names != expected:
            raise CmiLogPqStoreError(
                f"month file is not the canonical 74- or the legacy P=169 "
                f"71-column schema ({len(table.schema.names)} columns; "
                f"first: {table.schema.names[:5]})"
            )

    def write_month(
        self,
        month: str,
        records: list[tuple[str, dict[str, float | None]]],
    ) -> int:
        """Merge ``records`` into the month file (ts-de-duped) and rewrite it
        atomically.  Returns the number of NEW ts rows added.

        ``records`` are ``(ts, wide_row)`` pairs where ``wide_row`` maps wide
        column names -> value (``None`` = absent channel, all other columns
        NULL).  Only the month of ``records[0][0]`` may be written; a record
        from another month is a :class:`CmiLogPqStoreError` (the poller must
        not mix months into one rewrite).  A month file that already exists is
        read first; its rows are kept verbatim, and new records only fill ts
        values that are not present yet (existing rows win the de-duplication).
        The merged table is written to ``YYYY-MM.parquet.part`` and atomically
        renamed onto the month file; finished months are never touched because
        the poller only rewrites the current month.
        """
        if not month or not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
        if not records:
            return 0
        bad = [ts for ts, _ in records if ts[:7] != month]
        if bad:
            raise CmiLogPqStoreError(
                f"records carry timestamps outside month {month!r} (first: {bad[0]!r}); "
                "the live-month rewrite must not mix months"
            )

        dest = self._month_path(month)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Columnar read of the existing month (if any): {col -> [values]}.
        # The on-disk column set is checked against the canonical schema
        # BEFORE the read -- ``pq.read_table(..., schema=)`` would fill missing
        # columns with NULL and pass :meth:`_check_schema` silently, so a
        # non-canonical file must be rejected as a store error, never
        # overwritten.  The single documented exception: the legacy 71-column
        # P=169 month files (Q-125, missing the three dev2_analog_17/18/19
        # columns) are transparently migrated to the canonical 74-column schema
        # (the new columns read as NULL -- the P=169 records carried no
        # dev2 a17-a19 payload).
        cols: dict[str, list] = {}
        if dest.exists():
            on_disk = pq.read_schema(dest).names
            if on_disk == list(WIDE_COLUMNS):
                table = pq.read_table(dest, schema=_WIDE_SCHEMA)
                self._check_schema(table)
                cols = {c: table.column(c).to_pylist() for c in WIDE_COLUMNS}
            elif on_disk == [
                c for c in WIDE_COLUMNS if c not in _NEW_DEV2_ANALOG_COLUMNS
            ]:
                # Legacy P=169 month file (71 cols): read it under its own
                # schema, then pad the three new columns with NULL.  The data
                # is preserved verbatim; only the schema grows.
                legacy_schema = pa.schema([
                    ("ts", pa.string()),
                    *((c, pa.float64()) for c in on_disk if c != "ts"),
                ])
                table = pq.read_table(dest, schema=legacy_schema)
                self._check_legacy_schema(table)
                cols = {c: table.column(c).to_pylist() for c in on_disk}
                for c in _NEW_DEV2_ANALOG_COLUMNS:
                    cols[c] = [None] * len(table)
            else:
                raise CmiLogPqStoreError(
                    f"month file does not carry the canonical 74-column schema "
                    f"({len(on_disk)} columns, expected {len(WIDE_COLUMNS)}; "
                    f"first: {on_disk[:5]})"
                )

        # Append the new records (de-duplicated on ts; existing rows win).
        ts_new: list[str] = []
        wide_new: dict[str, list] = {c: [] for c in WIDE_COLUMNS[1:]}
        seen = set(cols.get("ts", ()))
        for ts, wide_row in records:
            if ts in seen:
                continue  # existing row wins (de-duplication on ts)
            seen.add(ts)
            ts_new.append(ts)
            for c in WIDE_COLUMNS[1:]:
                wide_new[c].append(wide_row.get(c))
        added = len(ts_new)
        if added:
            for c in WIDE_COLUMNS[1:]:
                cols.setdefault(c, []).extend(wide_new[c])
            cols["ts"] = (cols.get("ts", []) or []) + ts_new

        # Deterministic order: ts ascending.
        order = sorted(range(len(cols["ts"])), key=cols["ts"].__getitem__)
        table = pa.table(
            {c: [cols[c][i] for i in order] for c in WIDE_COLUMNS},
            schema=_WIDE_SCHEMA,
        )
        tmp = dest.with_suffix(".parquet.part")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, dest)  # atomic rename within the same directory
        self._refresh_probe()
        logger.debug("cmi-log pq: %s rewritten (%d rows, %d new)", dest, len(cols["ts"]), added)
        return added
