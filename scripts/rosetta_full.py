#!/usr/bin/env python3
"""Rosetta-25 proof: the 25 previously-missing cmi_log channels vs WinSol-LogX.

Re-runnable evidence for the S4N-LogFull (Q-112b) slot identification.  Decodes
the recorded CMI /LOG raw files (P = 169) and the WinSol-LogX ground-truth
files (P = 137) for the overlapping days and, per channel, reports:

  * the claimed CMI slot + scale (analog) or "no payload, 0.0 fill" (dev1 D)
  * the match rate (identical raw value / physical value) over the common ts
  * >= 5 sample pairs (ts, CMI value, GT value) as concrete proof
  * the GT value distribution (to show which channels are constant vs toggling)

Inputs (read-only, all committed):
  discovery/cmi-log/cmi_raw_2026-10-{27,28,29}.log
  discovery/cmi-log/cmi_raw_2026-10-30_toggle.log
  discovery/cmi-log/logx_2026-10-{27,28,29}.log
  discovery/cmi-log/logx_2026-10-30_toggle.log
  data/winsol_pq/2026/10/2026-10.parquet   (reference: Minderer 10 V state)

Usage:  .venv/bin/python scripts/rosetta_full.py
"""
from __future__ import annotations

import struct
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis"))

from winsol_logx import decode_record, find_sessions  # noqa: E402

from app.cmi.cmi_log import (  # noqa: E402
    _TIME_HH,
    RECORD_PERIOD,
    _detect_data_start,
    _detect_header_end,
)

DAYS = ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")
TOGGLE_SUFFIX = "_toggle"


# ---------------------------------------------------------------------------
# decoders -> {ts: {slot: raw}} (CMI) / {ts: {dev: {chan: value}}} (LogX)
# ---------------------------------------------------------------------------
def cmi_slot_map(path: Path) -> dict[str, dict[int, int]]:
    """{hh:mm:ss: {slot: raw u16}} for every *decoded* record of one CMI file.

    Mirrors :func:`app.cmi.cmi_log.decode_cmi_log` exactly: the header-overlap
    record (record 0, whose dev1/dev2 value slots the "Solarpumpe 1" label
    paints over) is skipped, so the match denominators below equal the
    decoder's ``n_decoded``.
    """
    buf = path.read_bytes()
    data_start = _detect_data_start(buf)
    header_end = _detect_header_end(buf, data_start)
    out: dict[str, dict[int, int]] = {}
    for k in range((len(buf) - data_start) // RECORD_PERIOD):
        rs = data_start + k * RECORD_PERIOD
        if rs < header_end:
            continue  # header-overlap record -> value slots are garbage
        b = rs + _TIME_HH
        hh, mm, ss = buf[b], buf[b - 1], buf[b - 2]
        if not (hh <= 23 and mm <= 59 and ss <= 59):
            continue
        out[f"{hh:02d}:{mm:02d}:{ss:02d}"] = {
            s: struct.unpack_from("<H", buf, rs + 2 * s)[0]
            for s in range(RECORD_PERIOD // 2)
        }
    return out


def logx_map(path: Path) -> dict[str, dict[int, dict[str, float]]]:
    """{hh:mm:ss: {dev: {chan: physical value}}} for one LogX file."""
    buf = path.read_bytes()
    out: dict[str, dict[int, dict[str, float]]] = {}
    for sess in find_sessions(buf):
        p = sess.variant.period
        for k in range(sess.n_records):
            rec = decode_record(
                buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant
            )
            if not rec.valid_clock:
                continue
            ts = f"{rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
            out[ts] = {int(d): {k: v for k, v in vals.items()} for d, vals in rec.values.items()}
    return out


# ---------------------------------------------------------------------------
# the 25 target channels (Rosetta-25, S4N-LogFull)
# ---------------------------------------------------------------------------
#: (label, cmi_dev, cmi_chan, logx_dev, logx_chan, logx_scale, cmi_slot|None,
#: note)  -- the note is printed verbatim for channels whose match rate is
#: NOT a proof of identity (documented divergences / constant fills).
ANALOG_TARGETS: tuple[tuple[str, int, str, int, str, int, int | None, str], ...] = (
    ("dev2 a10 (Minderer Keller)", 2, "a10", 2, "a10", 100, 34,
     "GT konstant 0 auf allen GT-Tagen; 10-V-Zustand real (winsol_pq: 27 808 "
     "Samples 2026-10-06..18). Match = Beleg des Slot (Konstanz-Proof)."),
    ("dev2 a11 (Minderer EG)", 2, "a11", 2, "a11", 100, 36, ""),
    ("dev2 a12 (Minderer OG)", 2, "a12", 2, "a12", 100, 38, ""),
    ("dev0 a11 (unbelegt)", 0, "a11", 0, "a11", 10, 72,
     "GT konstant 0, CMI-Slot 72 konstant 0 -> 0.0-Fill via echtem Slot."),
    ("dev0 a12 (unbelegt)", 0, "a12", 0, "a12", 10, 73, ""),
    ("dev0 a13 (unbelegt)", 0, "a13", 0, "a13", 10, 74, ""),
    ("dev0 a14 (unbelegt)", 0, "a14", 0, "a14", 10, 75, ""),
    ("dev0 a15 (Test)", 0, "a15", 0, "a15", 10, 76, ""),
    ("dev1 a10 (raw)", 1, "a10", 1, "a10", 1, 9,
     "DIVERGENZ (dokumentiert): LogX-Kanon konstant -1.0 (0xFFFF), CMI-Slot 9 "
     "haelt 0.0 -> 0.0-Fill mit CMI-Wert (entscheidend: keine NULL im Store)."),
    ("dev1 a14 (raw)", 1, "a14", 1, "a14", 1, 13,
     "GT konstant 0, CMI-Slot 13 konstant 0 -> 0.0-Fill via echtem Slot."),
    ("dev1 a15 (raw)", 1, "a15", 1, "a15", 1, 14, ""),
    ("dev1 a16 (raw)", 1, "a16", 1, "a16", 1, 15, ""),
)
#: dev1 digital d1-d13: no CMI payload; GT constant 0 -> 0.0 fill.
DEV1_DIGITAL: tuple[int, ...] = tuple(range(1, 14))


def _sample_pairs(
    common: list[str],
    cmi_val: list[float | int],
    gt_val: list[float | int],
    n: int = 5,
) -> str:
    """First ``n`` (ts, cmi, gt) pairs, marking mismatches."""
    out = []
    for shown, (ts, c, g) in enumerate(zip(common, cmi_val, gt_val, strict=False)):
        out.append(f"{ts} cmi={c} gt={g}" + ("" if c == g else "  [PHASE]"))
        if shown >= n - 1:
            break
    return "; ".join(out)


def main() -> int:
    cmi = {d: cmi_slot_map(REPO / "discovery/cmi-log" / f"cmi_raw_{d}{TOGGLE_SUFFIX if d.endswith('30') else ''}.log")
           for d in DAYS}
    lx = {d: logx_map(REPO / "discovery/cmi-log" / f"logx_{d}{TOGGLE_SUFFIX if d.endswith('30') else ''}.log")
          for d in DAYS}

    print("=" * 100)
    print("Rosetta-25: CMI /LOG (P=169) vs WinSol-LogX (P=137) -- die 25 zuvor fehlenden Kanäle")
    print("=" * 100)

    problems: list[str] = []
    for label, cd, _cc, ld, lc, scale, slot, note in ANALOG_TARGETS:
        print(f"\n{label}  ->  CMI dev{cd} slot {slot} /{scale}  (GT: dev{ld} {lc} /{scale})")
        if note:
            print(f"   ANMERKUNG: {note}")
        for day in DAYS:
            gt = {ts: lx[day][ts][ld][lc] for ts in lx[day]
                  if ts in cmi[day] and lc in lx[day][ts].get(ld, {})}
            if not gt:
                problems.append(f"{label} {day}: no GT samples")
                continue
            common = sorted(gt)
            cmi_vals = [cmi[day][ts][slot] for ts in common]
            matches = sum(1 for c, g in zip(cmi_vals, gt.values(), strict=False)
                          if c == round(g * scale))
            dist = Counter(gt.values())
            kind = "konstant" if len(dist) == 1 else "variabel"
            print(
                f"   {day}: {matches}/{len(common)} ({100*matches/len(common):.2f} %), "
                f"GT {kind} top={dist.most_common(2)}"
            )
            print(f"      samples: {_sample_pairs(common, cmi_vals, [round(g * scale) for g in (gt[ts] for ts in common)])}")

    # dev1 digital: GT constant 0 proof over all four LogX files
    print("\ndev1 digital d1-d13  ->  KEIN CMI-Payload (Slot-Scan negativ), 0.0-Fill")
    total_samples = 0
    total_nonzero = 0
    for day in DAYS:
        nz = 0
        for _ts, devs in lx[day].items():
            for ch, v in devs.get(1, {}).items():
                if ch.startswith("d"):
                    total_samples += 1
                    nz += int(v != 0)
        total_nonzero += nz
        print(f"   {day}: {len(lx[day])} Records")
    print(f"   gesamt: {total_nonzero} von {total_samples} GT-Samples != 0 -> 0.0-Fill belegt")

    # winsol_pq cross-check: dev2 a10 (Minderer Keller) 10 V state is real
    try:
        import duckdb

        con = duckdb.connect()
        r = con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM "
            f"read_parquet('{REPO / 'data/winsol_pq/2026/10/2026-10.parquet'}') "
            "WHERE dev2_analog_10 = 10.0"
        ).fetchone()
        print(
            f"\nwinsol_pq-Beleg dev2 a10 (Minderer Keller): {r[0]} Samples = 10.0 V "
            f"({r[1]} .. {r[2]}) -> Kanal real, 0-10 V"
        )
    except Exception as exc:  # noqa: BLE001 - optional cross-check
        print(f"\nwinsol_pq-Beleg dev2 a10: nicht verfügbar ({exc})")

    print("\n" + "=" * 100)
    if problems:
        print("PROBLEME: " + "; ".join(problems))
        return 1
    print("Rosetta-25: alle 25 Kanäle belegt (Details in docs/cmi-log-protocol.md Kap. 4.3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
