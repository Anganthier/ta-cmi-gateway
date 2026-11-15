#!/usr/bin/env python3
"""One-shot Remediation: 25 neue /LOG-Kanäle in den historischen NULL-Rows
des LIVE-Monats (2026-11) nachbefuellen, ohne den live wachsenden Monat zu
clobbern (S4N-LogFull, Folge des Gateway-Restarts 2026-11-02).

Hintergrund
-----------
Bevor das Gateway mit dem 70-Kanal-Decoder neu gestartet wurde, schrieb der
alte 45-Kanal-Decoder die 25 neuen Spalten als NULL:
  * 2026-11-01 17:06:39 .. 23:59:59
  * 2026-11-02 00:00:19 .. (Snapshot)
Diese historischen Rows koennen der Backfill-``write_month`` (Semantik
"existing rows win") und die Voll-Rewrite-TS-Safety ("alle Store-ts in der
Quelle") NICHT ersetzen, weil das Live-Monat waehrend des Backfills waechst.

Dieser Patch ist race-safe:
  * Er liest ein Store-Snapshot + die CMI-Roh-Quellen (11-01 + 11-02).
  * Er fuell NUR die 25 neuen Spalten an ts, die (a) im Store ein NULL in
    mindestens einer der 25 Spalten haben UND (b) in der Quelle mit
    70/70-Wert vorliegen.  Quelle gewinnt (CMI-Raw = autoritativ).
  * Alle anderen Store-Spalten/Rows bleiben unveraendert; Store-only-ts
    (neuere Live-Zeilen, nicht in der Quelle) werden erhalten.
  * Atomar: ``YYYY-MM.parquet.part`` -> rename, nur wenn der TS-Safety-Check
    (alle patch-Quellen-ts koennen im Store nachgewiesen werden) ok ist.

Nur 2026-11 wird angefasst; 2026-10 ist durch den regulaeeren Backfill
bereits 70/70 und bleibt unangetastet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis"))

from app.cmi.cmi_log import decode_cmi_log  # noqa: E402
from app.cmi.cmi_log_pq import WIDE_COLUMNS  # noqa: E402
from app.config import Settings  # noqa: E402

# Die 25 neuen Kanäle (S4N-LogFull).  Diese sind in den historischen
# NULL-Rows NULL und im neuen Decoder 70/70 befüllt.
NEW_25: tuple[str, ...] = tuple(
    [f"dev0_analog_{n:02d}" for n in range(11, 16)]
    + [f"dev1_analog_{n:02d}" for n in (10, 14, 15, 16)]
    + [f"dev1_digital_{n:02d}" for n in range(1, 14)]
    + [f"dev2_analog_{n:02d}" for n in (10, 11, 12)]
)
assert len(NEW_25) == 25, f"erwarte 25 neue Kanäle, habe {len(NEW_25)}"

# Die 45 Legacy-Kanäle = alle Value-Spalten minus NEW_25.
LEGACY_45: tuple[str, ...] = tuple(c for c in WIDE_COLUMNS[1:] if c not in NEW_25)
assert len(LEGACY_45) == 45, f"erwarte 45 Legacy, habe {len(LEGACY_45)}"

#: Quellen (CMI-Raw, natively 70-Kanal): 11-01 (vollständig) + 11-02 (wächst).
#: Die 11-02 ist "aktiv wachsend" -> kein Größen-Check, Snapshot wird
#: gelesen so, wie er zu Patch-Zeit auf der Platte liegt.
SOURCES: tuple[tuple[str, str], ...] = (
    ("data/cmi_log/data_2026_11_01_00_00_09.log", "data_2026_11_01_00_00_09.log"),
    ("data/cmi_log/data_2026_11_02_00_00_09.log", "data_2026_11_02_00_00_09.log"),
)


def _load_store(month_path: Path) -> tuple[dict[str, dict[str, float | None]], list[str]]:
    """Lies das Monats-Parquet in ein ts -> wide-Row-Map + Sortierung.

    Returns:
        (rows_by_ts, ts_order).  ``rows_by_ts`` hält alle 70 Value-Spalten
        als ``dict``; ``ts_order`` = ts in Schreibreihenfolge (Sortierung
        nach ts, da der Store ts-monoton appendet).
    """
    table = pq.read_table(month_path)
    names = table.schema.names
    assert names[0] == "ts", "erste Spalte muss 'ts' sein"
    ts_list = table.column("ts").to_pylist()
    cols: dict[str, list] = {c: table.column(c).to_pylist() for c in names[1:]}
    rows_by_ts: dict[str, dict[str, float | None]] = {}
    for i, ts in enumerate(ts_list):
        row: dict[str, float | None] = {}
        for c in WIDE_COLUMNS[1:]:
            row[c] = cols.get(c, [None] * len(ts_list))[i]
        rows_by_ts[ts] = row
    ts_order = sorted(rows_by_ts)
    return rows_by_ts, ts_order


def _decode_sources() -> dict[str, dict[str, float | None]]:
    """De-codiert die CMI-Raw-Quellen zu ts -> 70-Kanal-wide-Row (source-wins)."""
    merged: dict[str, dict[str, float | None]] = {}
    for rel, name in SOURCES:
        raw = (REPO / rel).read_bytes()
        data = decode_cmi_log(raw, filename=name)
        wide: dict[str, dict[str, float | None]] = {}
        for ts, device, channel, ad, value, _u, _s in data.rows:
            dev = device.rsplit(":", 1)[-1]
            kind = "analog" if ad == "A" else "digital"
            wide.setdefault(ts, {})[f"dev{dev}_{kind}_{int(channel):02d}"] = value
        for ts, row in wide.items():
            merged.setdefault(ts, row)  # 11-01 vor 11-02 (disjunkt, First-Wins)
    return merged


def _ts_safety(store_ts: set[str], source_ts: set[str],
               patch_ts: set[str]) -> tuple[bool, list[str]]:
    """Patch-Zeilen müssen im Store vorliegen (sonst Datenverlust).

    Returns:
        (ok, missing) -- ``missing`` = patch-ts, die im Store fehlen.
    """
    missing = sorted(patch_ts - store_ts)
    return (not missing, missing)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="nur analysieren, kein Parquet-Write")
    args = ap.parse_args(argv)

    settings = Settings()
    store_dir = Path(settings.cmi_log_parquet_dir)
    month = "2026-11"
    month_path = store_dir / "2026" / "11" / f"{month}.parquet"
    if not month_path.exists():
        print(f"ABBRUCH: Monats-File fehlt {month_path}")
        return 1

    print(f"Store: {month_path}")
    rows_by_ts, ts_order = _load_store(month_path)
    store_ts = set(rows_by_ts)
    print(f"Store 2026-11: {len(ts_order)} Rows, ts[{min(ts_order)} .. {max(ts_order)}]")

    # NULL-Rows identifizieren (mindestens eine der 25 Spalten NULL).
    null_rows = {
        ts for ts, row in rows_by_ts.items()
        if any(row.get(c) is None for c in NEW_25)
    }
    print(f"NULL-Rows (min. 1 der 25 neuen Spalten): {len(null_rows)}")
    if not null_rows:
        print("  → nichts zu tun (2026-11 ist bereits 70/70).")
        return 0

    src = _decode_sources()
    src_ts = set(src)
    patch_ts = null_rows & src_ts
    print(f"Quellen: {len(src_ts)} ts; davon in NULL-Rows: {len(patch_ts)} "
          f"(11-01 + 11-02 Snapshot)")

    ok, missing = _ts_safety(store_ts, src_ts, patch_ts)
    if not ok:
        print(f"ABBRUCH (TS-Safety): {len(missing)} patch-ts fehlen im Store "
              f"(erste: {missing[:3]}) -- Datenverlust ausgeschlossen")
        return 2

    # 70/70-Proof: jede patch-Quelle muss 70/70 sein (sonst füllen wir NULLs
    # mit NULLs).
    bad_src = 0
    for ts in patch_ts:
        row = src[ts]
        if any(row.get(c) is None for c in WIDE_COLUMNS[1:]):
            bad_src += 1
    if bad_src:
        print(f"ABBRUCH: {bad_src} Quellen-ts haben selbst NULLs (nicht 70/70)")
        return 2

    # Patch: für jedes patch-ts die 25 Spalten aus der Quelle übernehmen.
    n_patched = 0
    for ts in patch_ts:
        src_row = src[ts]
        store_row = rows_by_ts[ts]
        for c in NEW_25:
            store_row[c] = src_row.get(c)
        n_patched += 1
    print(f"Patched: {n_patched} ts (25 Spalten je ts, Quelle gewinnt)")

    # Verifikation: 70/70 über alle Store-Rows (auch die ungetuschten).
    still_null = 0
    for _ts, row in rows_by_ts.items():
        if any(row.get(c) is None for c in WIDE_COLUMNS[1:]):
            still_null += 1
    print(f"Nach Patch: {still_null} Rows mit ≥1 NULL (Ziel 0)")
    if still_null:
        sample = next(ts for ts, row in rows_by_ts.items()
                      if any(row.get(c) is None for c in WIDE_COLUMNS[1:]))
        print(f"  Beispiel: {sample}")
        print("ABBRUCH (nicht alle 70/70)")
        return 3

    if args.dry_run:
        print("Dry-Run: kein Parquet-Write.")
        return 0

    # Atomar schreiben: .part -> rename.
    part = month_path.with_suffix(".parquet.part")
    cols: dict[str, list] = {c: [] for c in WIDE_COLUMNS[1:]}
    ts_new: list[str] = []
    for ts in ts_order:
        ts_new.append(ts)
        row = rows_by_ts[ts]
        for c in WIDE_COLUMNS[1:]:
            cols[c].append(row.get(c))
    import pyarrow as pa
    arrays = [pa.array(ts_new, pa.string())]
    for c in WIDE_COLUMNS[1:]:
        arrays.append(pa.array(cols[c], pa.float64()))
    table = pa.Table.from_arrays(arrays, names=WIDE_COLUMNS)
    pq.write_table(table, part)
    part.replace(month_path)
    print(f"Atomar geschrieben: {month_path} ({len(ts_order)} Rows, 70/70)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
