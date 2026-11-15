#!/usr/bin/env python3
"""WinSol-LogX → SQLite-Import (S2-2).

.. note::
   **Superseded** by :mod:`analysis.winsol_to_parquet` (Parquet ist jetzt die
   kanonische Form).  Dieses Tool bleibt nur als inkrementeller Pfad (LogX →
   SQLite) erhalten, damit neue LogX-Tage neu dekodiert und per
   ``winsol_to_parquet.py --rebuild-month`` neu exportiert werden koennen.

Importiert den LogX-Bestand (2020..2026) nach data/winsol_import.db.
Idempotent: pro Datei werden die alten Zeilen geloescht und neu geschrieben.

Schema (Wide, 1 Zeile pro Record / 10 s):
    measurements(file, day, ts, year, hh, mm, ss,
                 dev0_analog_01..16, dev0_digital_02..09,
                 dev1_analog_01..16, dev1_digital_01..13,
                 dev2_analog_01..16, dev2_digital_01)
    files(name, year, day, period, anzdev, n_records, size, imported_at)

Aufruf:
    python analysis/import_sqlite.py                     # ganzer Bestand
    python analysis/import_sqlite.py --year 2023         # nur ein Jahr
    python analysis/import_sqlite.py --files f1 f2 ...   # konkrete Dateien
    python analysis/import_sqlite.py --limit 20          # max. 20 Dateien
    python analysis/import_sqlite.py --verify-csv AE2023-01-26_080759.csv D2023-01-26.log
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from winsol_logx import (  # noqa: E402
    Record,
    day_from_filename,
    decode_record,
    find_sessions,
    read_csv,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path("/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/LogX")
DEFAULT_DB = REPO_ROOT / "data" / "winsol_import.db"
DEFAULT_CSV_DIR = Path("/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/csv")

#: Spalten, die pro Datei geschrieben werden (analog nach Reihenfolge der Field-Listen)
DEV0_ANA_COLS = [f"dev0_analog_{i:02d}" for i in range(1, 17)]
DEV0_DIG_COLS = [f"dev0_digital_{i:02d}" for i in range(2, 10)]
DEV1_ANA_COLS = [f"dev1_analog_{i:02d}" for i in range(1, 17)]
DEV1_DIG_COLS = [f"dev1_digital_{i:02d}" for i in range(1, 14)]
DEV2_ANA_COLS = [f"dev2_analog_{i:02d}" for i in range(1, 17)]
DEV2_DIG_COLS = ["dev2_digital_01"]

ALL_VALUE_COLS = tuple(
    DEV0_ANA_COLS + DEV0_DIG_COLS + DEV1_ANA_COLS + DEV1_DIG_COLS + DEV2_ANA_COLS + DEV2_DIG_COLS
)
META_COLS = ("file", "day", "ts", "year", "hh", "mm", "ss")
ALL_COLS = META_COLS + ALL_VALUE_COLS

DEV0_DIG_MAP = {f"d{i}": f"dev0_digital_{i:02d}" for i in range(2, 10)}
DEV1_DIG_MAP = {f"d{i}": f"dev1_digital_{i:02d}" for i in range(1, 14)}


def _csv_index_map() -> dict[str, int]:
    """DB-Spalte -> CSV-Spalte (WinSol-Export, 93 Spalten; Drz/kW-Blöcke nicht importiert)."""
    m: dict[str, int] = {}
    for i in range(1, 16):
        m[f"dev0_analog_{i:02d}"] = 1 + i          # 1/Ana1..15 -> CSV 2..16
    m["dev0_analog_16"] = 17                       # 1/Ana16 Stroemungsschalter
    for i in range(2, 10):
        m[f"dev0_digital_{i:02d}"] = 25 + i        # 1/Dig2..9 -> CSV 27..34
    for i in range(1, 17):
        m[f"dev1_analog_{i:02d}"] = 38 + i         # 2/Ana1..16 -> CSV 39..54
    for i in range(1, 14):
        m[f"dev1_digital_{i:02d}"] = 62 + i        # 2/Dig1..13 -> CSV 63..75
    for i in range(1, 17):
        m[f"dev2_analog_{i:02d}"] = 75 + i         # 3/Ana1..16 -> CSV 76..91
    m["dev2_digital_01"] = 92                      # 3/Dig1
    return m


CSV_IDX = _csv_index_map()
ANALOG_TOL = 0.051  # CSV ruetzt auf 1 Dez. (x10-Kanael) / 2 Dez. (x100-Kanael)


def create_schema(conn: sqlite3.Connection) -> None:
    cols = ", ".join(f"{c} REAL" if "analog" in c else f"{c} INTEGER" for c in ALL_VALUE_COLS)
    conn.executescript(
        f"""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS measurements (
            file TEXT NOT NULL,
            day TEXT NOT NULL,
            ts TEXT NOT NULL,
            year INTEGER NOT NULL,
            hh INTEGER NOT NULL,
            mm INTEGER NOT NULL,
            ss INTEGER NOT NULL,
            {cols}
        );
        CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(ts);
        CREATE INDEX IF NOT EXISTS idx_measurements_file ON measurements(file);
        CREATE TABLE IF NOT EXISTS files (
            name TEXT PRIMARY KEY,
            year INTEGER,
            day TEXT,
            period INTEGER,
            anzdev INTEGER,
            n_records INTEGER,
            size INTEGER,
            imported_at TEXT DEFAULT (datetime('now'))
        );
        """
    )


def record_to_row(rec: Record, file_name: str) -> tuple:
    v0 = rec.values.get(0, {})
    v1 = rec.values.get(1, {})
    v2 = rec.values.get(2, {})
    row: list = [
        file_name,
        rec.day,
        rec.ts_str(),
        int((rec.day or "0000-00-00")[:4]),
        rec.hh,
        rec.mm,
        rec.ss,
    ]
    for col in DEV0_ANA_COLS:
        i = int(col.rsplit("_", 1)[1])
        row.append(v0.get(f"a{i}"))
    for col in DEV0_DIG_COLS:
        i = int(col.rsplit("_", 1)[1])
        row.append(v0.get(f"d{i}"))
    for col in DEV1_ANA_COLS:
        i = int(col.rsplit("_", 1)[1])
        row.append(v1.get(f"a{i}"))
    for col in DEV1_DIG_COLS:
        i = int(col.rsplit("_", 1)[1])
        row.append(v1.get(f"d{i}"))
    for col in DEV2_ANA_COLS:
        i = int(col.rsplit("_", 1)[1])
        row.append(v2.get(f"a{i}"))
    row.append(v2.get("d1"))
    return tuple(row)


def import_file(conn: sqlite3.Connection, path: Path, verbose: bool = True) -> tuple[int, int, int, int]:
    """Eine Datei importieren; liefert (period, anzdev, n_records, fehlerfrei).

    Multi-Session-Dateien (Logger-Neustart => mehrere '>>>>'-Header, teils
    verschiedene Perioden) werden pro Session korrekt dekodiert.
    """
    buf = path.read_bytes()
    day = day_from_filename(path)
    if day is None:
        if verbose:
            print(f"  WARN: Datum nicht aus Dateinamen, verwerfe Datei: {path.name}", file=sys.stderr)
        return 0, 0, 0, 0
    sessions = find_sessions(buf)
    dominant = max(sessions, key=lambda s: s.n_records).variant
    rows: list[tuple] = []
    skipped = 0
    for sess in sessions:
        p = sess.variant.period
        for k in range(sess.n_records):
            rec = decode_record(buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant, day)
            if not rec.valid_clock:
                skipped += 1  # abgerissene/korrumpierte Record (z. B. Session-Ende)
                continue
            rows.append(record_to_row(rec, path.name))
    cur = conn.cursor()
    cur.execute("DELETE FROM measurements WHERE file = ?", (path.name,))
    cur.executemany(
        f"INSERT INTO measurements ({', '.join(ALL_COLS)}) VALUES ({', '.join('?' for _ in ALL_COLS)})",
        rows,
    )
    cur.execute(
        "INSERT OR REPLACE INTO files (name, year, day, period, anzdev, n_records, size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (path.name, int(day[:4]), day, dominant.period, dominant.anzdev, len(rows), len(buf)),
    )
    conn.commit()
    if verbose:
        multi = f" ({len(sessions)} Sessions: " + ", ".join(str(s.variant.period) for s in sessions) + ")" if len(sessions) > 1 else ""
        skip = f" skipped={skipped}" if skipped else ""
        print(
            f"  ok {path.name}: P={dominant.period} dev={dominant.anzdev} "
            f"records={len(rows)}{skip} ({len(buf)//1024} kB){multi}"
            + ("  [WARN: unvollstaendig, <8640]" if len(rows) < 8600 else ""),
            file=sys.stderr,
        )
    return dominant.period, dominant.anzdev, len(rows), 1


def find_files(
    src: Path, years: set[str] | None, limit: int | None
) -> list[Path]:
    files: list[Path] = []
    for y in sorted(os.listdir(src)):
        if not y.isdigit():
            continue
        if years and y not in years:
            continue
        ydir = src / y
        if ydir.is_dir():
            files.extend(sorted(p for p in ydir.iterdir() if p.suffix == ".log"))
    if limit:
        files = files[:limit]
    return files


def verify_against_csv(conn: sqlite3.Connection, csv_path: Path, log_path: Path) -> int:
    """Rosetta-Check: CSV-Zeilen vs. DB-Zeilen der LogX-Datei.

    Vergleicht jeden importierten Kanal mit dem WinSol-CSV-Export.
    Analog-Kanael: Toleranz 0.051 (CSV-Ruendung auf 1/2 Dezimalstellen);
    Digital/Flag: exakt.
    """
    day = day_from_filename(log_path)
    assert day is not None, f"kein Datum in {log_path.name}"
    hdr, rows = read_csv(csv_path, day=day)
    q = (
        "SELECT " + ", ".join(ALL_VALUE_COLS)
        + " FROM measurements WHERE file = ? AND day = ? AND hh=? AND mm=? AND ss=?"
    )
    bad = 0
    checked = 0
    for row in rows:
        hh, mm, ss = map(int, row[1].split(":"))
        cur = conn.execute(q, (log_path.name, day, hh, mm, ss))
        db = cur.fetchone()
        if db is None:
            bad += 1
            print(f"  FEHLT in DB: {day} {row[1]}", file=sys.stderr)
            continue
        for col, db_val in zip(ALL_VALUE_COLS, db, strict=True):
            csv_col = CSV_IDX.get(col)
            if csv_col is None:
                continue
            raw_val = row[csv_col].strip() if csv_col < len(row) else ""
            if not raw_val:
                continue  # CSV-Spalte leer (z. B. Drz/kW-Blöcke)
            if db_val is None:
                bad += 1
                print(f"  {col}: DB=NULL, CSV={raw_val!r} @ {row[1]}", file=sys.stderr)
                continue
            if col.endswith("analog_16") and col.startswith("dev0_") or "digital" in col:
                if int(db_val) != int(float(raw_val)):
                    bad += 1
                    print(f"  {col}: DB={db_val}, CSV={raw_val!r} @ {row[1]}", file=sys.stderr)
            else:
                if abs(float(db_val) - float(raw_val)) > ANALOG_TOL:
                    bad += 1
                    print(f"  {col}: DB={db_val}, CSV={raw_val!r} @ {row[1]}", file=sys.stderr)
        checked += 1
    print(f"Verify {log_path.name} vs {csv_path.name}: {checked} Zeilen, {bad} Abweichungen")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WinSol LogX → SQLite")
    ap.add_argument("files", nargs="*", help="konkrete LogX-Dateien (sonst: ganzer Bestand)")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="LogX-Quellverzeichnis")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite-Ziel (Default data/winsol_import.db)")
    ap.add_argument("--year", action="append", help="nur dieses Jahr (wiederholbar)")
    ap.add_argument("--limit", type=int, help="max. Anzahl Dateien")
    ap.add_argument("--verify-csv", nargs=2, metavar=("CSV", "LOG"),
                    help="nur Cross-Check CSV vs. importierter Log-Datei")
    args = ap.parse_args(argv)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    create_schema(conn)

    if args.verify_csv:
        csv_path, log_path = Path(args.verify_csv[0]), Path(args.verify_csv[1])
        if not log_path.exists() or not csv_path.exists():
            print("CSV/Log-Datei fehlt", file=sys.stderr)
            return 2
        return verify_against_csv(conn, csv_path, log_path)

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        years = set(args.year or [])
        files = find_files(args.src, years or None, args.limit)
    if not files:
        print("keine Dateien gefunden", file=sys.stderr)
        return 2

    t0 = time.time()
    total = 0
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f}", file=sys.stderr)
        try:
            _, _, n, ok = import_file(conn, f, verbose=True)
            total += n if ok else 0
        except Exception as e:  # noqa: BLE001
            print(f"  FEHLER {f.name}: {e}", file=sys.stderr)
    print(f"\nDone: {len(files)} Dateien, {total} Records importiert, {time.time()-t0:.0f} s", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
