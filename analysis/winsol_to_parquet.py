#!/usr/bin/env python3
"""WinSol SQLite wide store -> Parquet (canonical) export (S2-Parquet-Pivot).

DECISION (human-approved): Parquet is the SINGLE canonical form for the WinSol
history.  This tool exports the 8 GB wide SQLite store ``data/winsol_import.db``
into Hive-partitioned, zstd-compressed Parquet::

    data/winsol_pq/YYYY/MM/YYYY-MM.parquet

The Parquet schema keeps only ``ts`` (canonical ``'YYYY-MM-DD HH:MM:SS'`` STRING,
naive local time -- deliberately NOT converted to a timestamp/timezone) plus the
70 value columns ``dev{0,1,2}_analog_{01..16}`` and
``dev{0,1,2}_digital_{01..13}``.  The meta columns ``file/day/year/hh/mm/ss``
are dropped (they are derivable from ``ts``).

* The source is read **read-only** via DuckDB's native SQLite scanner
  (``sqlite_scan``); the original WinSol ``.log`` files are never touched and no
  LogX re-decode is needed (the SQLite import was already 100% cross-validated
  against the WinSol CSVs).
* **Chunk-safe**: rows are grouped by month and one Parquet file is written per
  month (``COPY ... TO ... FORMAT PARQUET, COMPRESSION ZSTD``).  Each month is
  an atomic DuckDB statement, so a crash can never corrupt already-finished
  months and re-runs are idempotent (a month is simply rewritten).
* After the export a **built-in verification** runs (same code as ``--verify``):
  total + per-month row counts must match the source exactly, and a CSV
  Rosetta spot-check compares Parquet values against two WinSol CSV
  ground-truth files (>=50 value points across dev0/dev1/dev2; analog tolerance
  +-0.5 K, digital exact).  Any mismatch is an export-pipeline bug and FAILs.
  GROUND TRUTH: the WinSol CSVs are UTF-32LE (BOM FF FE 00 00), NOT UTF-16LE as
  older project docs claimed.

Incremental path (NEW LogX days later): re-decode via ``analysis/winsol_logx.py``
+ ``analysis/import_sqlite.py`` (or directly), then re-run this tool with
``--rebuild-month YYYY-MM`` to rewrite just that month's file.

Usage::

    # full export + verify:
    python analysis/winsol_to_parquet.py
    # rebuild a single month:
    python analysis/winsol_to_parquet.py --rebuild-month 2023-01
    # verification report only (no export):
    python analysis/winsol_to_parquet.py --verify
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO_ROOT / "data" / "winsol_import.db"
DEFAULT_DST = REPO_ROOT / "data" / "winsol_pq"
#: The two CSV ground-truth files used for the Rosetta spot-check (read-only).
DEFAULT_CSV_DIR = Path(
    "/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/csv"
)
DEFAULT_ROSETTA_CSVS = (
    "AE2023-01-26_080759.csv",  # full-day Rosetta day
    "AE2022-04-04_152304.csv",  # second, independent day
)

# ---------------------------------------------------------------------------
# Wide schema (must mirror data/winsol_import.db / the Parquet target).
# ---------------------------------------------------------------------------
DEV0_ANA_COLS = [f"dev0_analog_{i:02d}" for i in range(1, 17)]
DEV0_DIG_COLS = [f"dev0_digital_{i:02d}" for i in range(2, 10)]
DEV1_ANA_COLS = [f"dev1_analog_{i:02d}" for i in range(1, 17)]
DEV1_DIG_COLS = [f"dev1_digital_{i:02d}" for i in range(1, 14)]
DEV2_ANA_COLS = [f"dev2_analog_{i:02d}" for i in range(1, 17)]
DEV2_DIG_COLS = ["dev2_digital_01"]
ALL_VALUE_COLS: tuple[str, ...] = tuple(
    DEV0_ANA_COLS + DEV0_DIG_COLS + DEV1_ANA_COLS + DEV1_DIG_COLS + DEV2_ANA_COLS + DEV2_DIG_COLS
)


def _project_cols() -> str:
    """ts (as-is STRING) + every value column cast to DOUBLE (uniform, NULL-safe)."""
    return "ts, " + ", ".join(f"CAST({c} AS DOUBLE) AS {c}" for c in ALL_VALUE_COLS)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _src_total_rows(con: duckdb.DuckDBPyConnection, src: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM sqlite_scan('{src}', 'measurements')").fetchone()[0])


def _src_per_month(con: duckdb.DuckDBPyConnection, src: str) -> dict[str, int]:
    rows = con.execute(
        f"SELECT substr(day, 1, 7) AS m, COUNT(*) FROM sqlite_scan('{src}', 'measurements') "
        "GROUP BY m"
    ).fetchall()
    return {m: int(n) for m, n in rows}


def _pq_month_path(dst: Path, month: str) -> Path:
    return dst / f"{month[:4]}" / f"{month[5:7]}" / f"{month}.parquet"


def _write_month(con: duckdb.DuckDBPyConnection, src: str, dst: Path, month: str) -> int:
    """Atomically (re)write one month's Parquet file; return its row count."""
    out = _pq_month_path(dst, month)
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT {_project_cols()} FROM sqlite_scan('{src}', 'measurements') "
        f"WHERE substr(day, 1, 7) = '{month}' ORDER BY ts) "
        f"TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0])


def _pq_per_month(con: duckdb.DuckDBPyConnection, dst: Path, months: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in months:
        p = _pq_month_path(dst, m)
        if not p.is_file():
            continue
        counts[m] = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{p}')").fetchone()[0])
    return counts


def _pq_total(con: duckdb.DuckDBPyConnection, dst: Path) -> int:
    files = sorted(dst.glob("*/*/*.parquet"))
    if not files:
        return 0
    return int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst}/*/*/*.parquet')").fetchone()[0])


# ---------------------------------------------------------------------------
# CSV Rosetta spot-check
# ---------------------------------------------------------------------------
def _csv_index_map() -> dict[str, int]:
    """Parquet column -> WinSol CSV column index (93-col export)."""
    m: dict[str, int] = {}
    for i in range(1, 16):
        m[f"dev0_analog_{i:02d}"] = 1 + i
    m["dev0_analog_16"] = 17
    for i in range(2, 10):
        m[f"dev0_digital_{i:02d}"] = 25 + i
    for i in range(1, 17):
        m[f"dev1_analog_{i:02d}"] = 38 + i
    for i in range(1, 14):
        m[f"dev1_digital_{i:02d}"] = 62 + i
    for i in range(1, 17):
        m[f"dev2_analog_{i:02d}"] = 75 + i
    m["dev2_digital_01"] = 92
    return m


CSV_IDX = _csv_index_map()
ANALOG_TOL = 0.5  # K (Rosetta: +-0.5 K for temperatures)


def _read_winsol_csv(path: Path, day: str) -> list[list[str]]:
    """Read the CSV rows for ``day`` (UTF-32LE BOM, tab-separated, col0=Datum)."""
    raw = path.read_bytes()
    text = raw.decode("utf-32-le")
    rows: list[list[str]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip()[:4].isdigit():
            continue
        if parts[0].strip() == day:
            rows.append(parts)
    return rows


def _csv_day(name: str) -> str | None:
    """Extract the ``YYYY-MM-DD`` day from a WinSol CSV/log file name.

    Handles both prefixes seen on disk: 1-char log files (``D2023-01-26.log``)
    and 2-char CSV exports (``AE2023-01-26_080759.csv``) -- a plain ``[1:11]``
    slice would be off by one for the 2-char prefix.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None


def _load_pq_day(con: duckdb.DuckDBPyConnection, dst: Path, day: str) -> dict[str, dict[str, object]]:
    """Load one day's Parquet rows keyed by ``ts`` (for the Rosetta check)."""
    p = _pq_month_path(dst, day[:7])
    if not p.is_file():
        raise SystemExit(f"  FEHLENDE Monatsdatei fuer Rosetta-Check: {p}")
    cols = ", ".join(ALL_VALUE_COLS)
    rows = con.execute(
        f"SELECT ts, {cols} FROM read_parquet('{p}') WHERE substr(ts, 1, 10) = '{day}'"
    ).fetchall()
    lookup: dict[str, dict[str, object]] = {}
    for r in rows:
        lookup[r[0]] = {c: v for c, v in zip(ALL_VALUE_COLS, r[1:], strict=True)}
    return lookup


@dataclass
class RosettaResult:
    ok: bool
    csv_files: int = 0
    rows_sampled: int = 0
    points_checked: int = 0
    points_bad: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return (self.points_checked - self.points_bad) / self.points_checked if self.points_checked else 0.0


def verify_csv_rossetta(
    con: duckdb.DuckDBPyConnection, dst: Path, csv_paths: list[Path], *, sample_step: int = 29
) -> RosettaResult:
    """Compare Parquet values against WinSol CSV ground-truth rows.

    Samples ``sample_step``-th CSV rows (>= ~50 points/device), and for each
    sampled row compares every mapped value column.  Analog channels use a
    +-0.5 K tolerance; digital/flag columns must match exactly.  A single
    mismatch is an export-pipeline bug -> ``ok`` stays False.
    """
    res = RosettaResult(ok=True)
    for csv_path in csv_paths:
        day = _csv_day(csv_path.name)
        if day is None:
            res.details.append(f"WARN {csv_path.name}: kein Datum im Namen erkennbar")
            res.ok = False
            continue
        rows = _read_winsol_csv(csv_path, day)
        if not rows:
            res.details.append(f"WARN {csv_path.name}: keine Zeilen fuer {day}")
            res.ok = False
            continue
        sample = rows[:: sample_step]
        pq = _load_pq_day(con, dst, day)
        res.csv_files += 1
        res.rows_sampled += len(sample)
        for row in sample:
            hh, mm, ss = row[1].split(":")
            ts = f"{day} {int(hh):02d}:{int(mm):02d}:{int(ss):02d}"
            rec = pq.get(ts)
            if rec is None:
                res.points_bad += 1
                res.ok = False
                res.details.append(f"FEHLT in Parquet: {ts} ({csv_path.name})")
                continue
            for col, csv_col in CSV_IDX.items():
                if csv_col >= len(row):
                    continue
                raw = row[csv_col].strip()
                if not raw:
                    continue
                got = rec.get(col)
                if got is None:
                    res.points_bad += 1
                    res.ok = False
                    res.details.append(f"{col}: Parquet=NULL, CSV={raw!r} @ {ts}")
                    continue
                if "digital" in col:
                    if int(got) != int(float(raw)):
                        res.points_bad += 1
                        res.ok = False
                        res.details.append(f"{col}: Parquet={got}, CSV={raw!r} @ {ts}")
                elif abs(float(got) - float(raw)) > ANALOG_TOL:
                    res.points_bad += 1
                    res.ok = False
                    res.details.append(
                        f"{col}: Parquet={got}, CSV={float(raw):.2f} @ {ts} "
                        f"(diff {abs(float(got) - float(raw)):.3f} > {ANALOG_TOL})"
                    )
                res.points_checked += 1
    if res.points_checked == 0:
        res.ok = False
        res.details.append(
            "FAKE-PASS: 0 value points checked (no CSV rows parsed / no Parquet day "
            "loaded) -- the Rosetta check could not actually verify anything"
        )
    return res


# ---------------------------------------------------------------------------
# Verification report
# ---------------------------------------------------------------------------
@dataclass
class VerifyResult:
    ok: bool
    src_total: int
    pq_total: int
    months_compared: int
    months_mismatch: int
    missing_months: int
    per_month_mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    csv: RosettaResult | None = None

    def report(self) -> str:
        lines = [
            "=" * 70,
            "WinSol Parquet EXPORT-VERIFICATION",
            "=" * 70,
        ]
        if self.src_total >= 0:
            lines.append(f"source rows (sqlite)   : {self.src_total:,}")
        else:
            lines.append("source rows (sqlite)   : n/a (source already deleted)")
        lines.append(f"parquet rows (total)   : {self.pq_total:,}")
        if self.src_total >= 0:
            lines.append(
                f"row-count match        : {'YES' if self.src_total == self.pq_total else 'NO'}"
            )
            lines.append(f"months compared        : {self.months_compared}")
            lines.append(f"missing month files    : {self.missing_months}")
            lines.append(f"per-month mismatches   : {len(self.per_month_mismatches)}")
        for m, s, p in self.per_month_mismatches:
            lines.append(f"  MISMATCH {m}: src={s:,} pq={p:,}")
        if self.csv is not None:
            c = self.csv
            lines.append("-" * 70)
            lines.append(f"CSV Rosetta spot-check     : {'PASS' if c.ok else 'FAIL'}")
            lines.append(f"  csv files                : {c.csv_files}")
            lines.append(f"  rows sampled             : {c.rows_sampled}")
            lines.append(f"  value points checked     : {c.points_checked}")
            lines.append(f"  value points mismatched  : {c.points_bad}")
            lines.append(f"  match rate               : {c.match_rate * 100:.2f}%")
            for d in c.details[:20]:
                lines.append(f"    {d}")
            if len(c.details) > 20:
                lines.append(f"    ... (+{len(c.details) - 20} more)")
        lines.append("=" * 70)
        lines.append(f"RESULT: {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


def run_verification(
    con: duckdb.DuckDBPyConnection, src: Path, dst: Path, csv_paths: list[Path]
) -> VerifyResult:
    """Verify the Parquet store.

    When the SQLite source is still present (normal export path) the total and
    every month's row count are compared against it.  When the source has
    already been deleted (post-deletion ``--verify``) the source row-count
    checks are skipped and only the Parquet total + the CSV Rosetta spot-check
    are reported.
    """
    src_present = src.is_file()
    pq_total = _pq_total(con, dst)
    csv_res = verify_csv_rossetta(con, dst, csv_paths)
    if src_present:
        months = sorted(_src_per_month(con, str(src)).keys())
        src_total = _src_total_rows(con, str(src))
        pq_months = _pq_per_month(con, dst, months)
        mismatches: list[tuple[str, int, int]] = []
        missing = 0
        for m in months:
            s = int(con.execute(
                f"SELECT COUNT(*) FROM sqlite_scan('{src}', 'measurements') "
                f"WHERE substr(day,1,7)='{m}'"
            ).fetchone()[0])
            p = pq_months.get(m, 0)
            if m not in pq_months:
                missing += 1
                mismatches.append((m, s, 0))
            elif s != p:
                mismatches.append((m, s, p))
        ok = (
            src_total == pq_total
            and not mismatches
            and missing == 0
            and csv_res.ok
        )
        return VerifyResult(
            ok=ok,
            src_total=src_total,
            pq_total=pq_total,
            months_compared=len(months),
            months_mismatch=len(mismatches),
            missing_months=missing,
            per_month_mismatches=mismatches,
            csv=csv_res,
        )
    # Source already deleted: no row-count comparison possible.
    ok = csv_res.ok
    return VerifyResult(
        ok=ok,
        src_total=-1,
        pq_total=pq_total,
        months_compared=-1,
        months_mismatch=0,
        missing_months=0,
        per_month_mismatches=[],
        csv=csv_res,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WinSol SQLite wide store -> Parquet (canonical)")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="SQLite wide source (default data/winsol_import.db)")
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST, help="Parquet destination dir (default data/winsol_pq)")
    ap.add_argument("--rebuild-month", default=None, help="only (re)write this YYYY-MM month")
    ap.add_argument("--verify", action="store_true", help="run the verification report (always run after export)")
    ap.add_argument("--skip-csv", action="store_true", help="skip the CSV Rosetta spot-check")
    args = ap.parse_args(argv)

    src: Path = args.src
    dst: Path = args.dst
    dst.mkdir(parents=True, exist_ok=True)
    src_present = src.is_file()
    if not src_present and not args.verify:
        _log(f"FEHLER: source not found: {src} (use --verify to check the Parquet tree only)")
        return 2
    con = duckdb.connect()

    try:
        csv_paths = [DEFAULT_CSV_DIR / n for n in DEFAULT_ROSETTA_CSVS] if not args.skip_csv else []

        if args.rebuild_month:
            if not src_present:
                _log("FEHLER: --rebuild-month needs the source to be present")
                return 2
            months = sorted(_src_per_month(con, str(src)).keys())
            if args.rebuild_month not in months:
                _log(f"FEHLER: month {args.rebuild_month} not in source (have {len(months)} months)")
                return 2
            do_months = [args.rebuild_month]
        else:
            do_months = [] if not src_present else sorted(_src_per_month(con, str(src)).keys())

        if args.verify:
            _log("--verify: skipping export, running verification report only ...")
        else:
            _log(f"exporting {len(do_months)} month(s) from {src} -> {dst} (zstd Parquet)")
            t_all = time.time()
            for i, m in enumerate(do_months, 1):
                t0 = time.time()
                n = _write_month(con, str(src), dst, m)
                size = _pq_month_path(dst, m).stat().st_size
                _log(f"[{i}/{len(do_months)}] {m}: {n:,} rows, {size / 1048576:.1f} MB, {time.time() - t0:.1f}s")
            _log("-" * 70)
            _log(f"export done in {time.time() - t_all:.0f}s; running built-in verification ...")
        result = run_verification(con, src, dst, csv_paths)
        report = result.report()
        print(report)  # stdout for the operator
        (dst / "EXPORT_REPORT.txt").write_text(
            report + f"\n\nGenerated: {datetime.now(UTC).isoformat()}\n", encoding="utf-8"
        )
        return 0 if result.ok else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
