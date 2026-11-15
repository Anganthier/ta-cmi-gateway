"""Tests fuer den WinSol-LogX-Dekodierer (analysis/winsol_logx.py).

Laufweise:
    python3 analysis/test_decode.py          # standalone, ohne pytest
    pytest tests/test_winsol_logx.py         # ueber Repo-Pytest (importiert diese Datei)

Grundlage (Rosetta-Stein): WinSol-CSV-Exporte in discovery/winsol/
    D2023-01-26.log + AE2023-01-26_080759.csv   (P=131, 2928 Zeilen, 100 %-Abgleich)
    D2022-03-31.log + AE2022-03-31_082509.csv   (P=131, 409 Zeilen, 2. Verifikation)
    P=137-Files: /home/user/Documents/.../LogX/2026/ (read-only, strukturell)
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "analysis"))

from winsol_logx import (  # noqa: E402
    Record,
    day_from_filename,
    decode_dev0_digital,
    detect_variant,
    find_sessions,
    iter_records,
    read_csv,
)

try:
    import pytest
except ImportError:  # standalone (no pytest)
    pytest = None  # type: ignore[assignment]


def _need(*paths: Path) -> None:
    """Skip when a WinSol capture file is not available.

    The Rosetta captures (``discovery/winsol/*.log|csv``) and the host
    ``LogX`` ground truth are multi-MB artifacts kept out of the public
    repo (``> 1 MB`` / host-local). In the internal dev tree they exist and
    the tests run fully; elsewhere they are skipped.
    """
    if pytest is None:
        return
    for p in paths:
        if not p.is_file():
            pytest.skip(f"WinSol capture not available: {p.name}")

WIN = REPO_ROOT / "discovery" / "winsol"
LOG_2023 = WIN / "D2023-01-26.log"
CSV_2023 = WIN / "AE2023-01-26_080759.csv"
LOG_2022 = WIN / "D2022-03-31.log"
CSV_2022 = WIN / "AE2022-03-31_082509.csv"

SRC = Path("/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/LogX")
LOG_2026_P137 = SRC / "2026" / "D2026-03-01.log"
LOG_2022_MULTISESS = SRC / "2022" / "D2022-03-07.log"

# CSV-Spalten (0-basiert) — s. docs/winsol-logx.md
C_DEV0_ANA = list(range(2, 17))    # 1/Ana1..Ana15
C_DEV0_A16 = 17                    # Stroemungsschalter (0/1)
C_DEV0_DIG = list(range(26, 39))   # 1/Dig1..Dig13
C_DEV1_ANA = {  # 0-basierte CSV-Spalte -> (dev1-Feld, Skala)
    39: ("a1", 10), 40: ("a2", 10), 41: ("a3", 1), 42: ("a4", 10), 43: ("a5", 10),
    44: ("a6", 1), 45: ("a7", 10), 46: ("a8", 10), 47: ("a9", 1), 48: ("a10", 1),
    49: ("a11", 10), 50: ("a12", 10), 51: ("a13", 10), 52: ("a14", 1),
    53: ("a15", 1), 54: ("a16", 1),
}
C_DEV1_DIG = list(range(63, 76))   # 2/Dig1..Dig13
C_DEV2_ANA = {
    76: ("a1", 10), 77: ("a2", 10), 78: ("a3", 10), 79: ("a4", 10), 80: ("a5", 10),
    81: ("a6", 10), 82: ("a7", 10), 83: ("a8", 10), 84: ("a9", 10),
    85: ("a10", 100), 86: ("a11", 100), 87: ("a12", 100),
    88: ("a13", 10), 89: ("a14", 10), 90: ("a15", 10), 91: ("a16", 10),
}
C_DEV2_DIG = 92                    # 3/Dig1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _rows2023() -> list[list[str]]:
    hdr, rows = read_csv(CSV_2023, day="2023-01-26")
    assert hdr[0] == "Datum", repr(hdr[0])
    return rows


def _recs2023(n: int | None = None):
    buf = LOG_2023.read_bytes()
    v = detect_variant(buf)
    day = day_from_filename(LOG_2023)
    return list(iter_records(buf, v, day, limit=n))


# ---------------------------------------------------------------------------
# Variante-Erkennung
# ---------------------------------------------------------------------------
def test_variant_p131_detected() -> None:
    _need(LOG_2023)
    v = detect_variant(LOG_2023.read_bytes())
    assert (v.period, v.anzdev, v.data_offset) == (131, 3, 197), v
    assert v.dev2, "P=137/131: dev2-Block muss spezifiziert sein"


def test_variant_p137_detected() -> None:
    _need(LOG_2026_P137)
    v = detect_variant(LOG_2026_P137.read_bytes())
    assert (v.period, v.anzdev, v.data_offset) == (137, 3, 197), v
    # dev2 a10..a12 breit (u32)
    widths = {f.ch: f.width for f in v.dev2}
    assert widths["a10"] == widths["a11"] == widths["a12"] == 4, widths
    assert widths["a16"] == 2


def test_day_from_filename() -> None:
    assert day_from_filename(LOG_2023) == "2023-01-26"
    assert day_from_filename("AE2023-01-26_080759.csv") is None


def test_read_csv_utf32() -> None:
    _need(CSV_2023)
    hdr, rows = read_csv(CSV_2023, day="2023-01-26")
    assert len(hdr) == 93, len(hdr)
    assert len(rows) == 2928, len(rows)
    assert rows[0][:2] == ["2023-01-26", "00:00:03"]


# ---------------------------------------------------------------------------
# P=131 vs CSV: 100 %-Abgleich (Rosetta-Stein)
# ---------------------------------------------------------------------------
def test_clock_matches_csv() -> None:
    _need(LOG_2023, CSV_2023)
    rows = _rows2023()
    recs = _recs2023(len(rows))
    for k, (row, rec) in enumerate(zip(rows, recs, strict=True)):
        hh, mm, ss = map(int, row[1].split(":"))
        assert (rec.hh, rec.mm, rec.ss) == (hh, mm, ss), (k, row[1], rec.clock)


def test_dev0_analog_matches_csv() -> None:
    _need(LOG_2023, CSV_2023)
    rows = _rows2023()
    recs = _recs2023(len(rows))
    for k, (row, rec) in enumerate(zip(rows, recs, strict=True)):
        for j, ch in enumerate(f"a{i}" for i in range(1, 16)):
            col = C_DEV0_ANA[j]
            want = int(round(float(row[col]) * 10))
            got = rec.values[0][ch]
            assert round(got * 10) == want, (k, ch, row[col], got)
        # a16 = 0/1-Flag
        assert rec.values[0]["a16"] == (1 if row[C_DEV0_A16].strip() == "1" else 0), k


def test_dev0_digital_permuted_matches_csv() -> None:
    _need(LOG_2023, CSV_2023)
    rows = _rows2023()
    recs = _recs2023(len(rows))
    for k, (row, rec) in enumerate(zip(rows, recs, strict=True)):
        for key in sorted(decode_dev0_digital(0)):
            col = C_DEV0_DIG[int(key[1:]) - 1]
            want = 1 if row[col].strip() == "1" else 0
            assert rec.values[0][key] == want, (k, key, row[col], rec.values[0][key])


def test_dev1_analog_matches_csv() -> None:
    _need(LOG_2023, CSV_2023)
    rows = _rows2023()
    recs = _recs2023(len(rows))
    for k, (row, rec) in enumerate(zip(rows, recs, strict=True)):
        for col, (ch, scale) in C_DEV1_ANA.items():
            want = int(round(float(row[col]) * scale))
            got = rec.values[1][ch]
            assert round(got * scale) == want, (k, ch, row[col], got)
        for j in range(13):
            col = C_DEV1_DIG[j]
            want = 1 if row[col].strip() == "1" else 0
            assert rec.values[1][f"d{j + 1}"] == want, (k, j, want)


def test_dev2_analog_matches_csv() -> None:
    _need(LOG_2023, CSV_2023)
    rows = _rows2023()
    recs = _recs2023(len(rows))
    for k, (row, rec) in enumerate(zip(rows, recs, strict=True)):
        for col, (ch, scale) in C_DEV2_ANA.items():
            want = int(round(float(row[col]) * scale))
            got = rec.values[2][ch]
            assert round(got * scale) == want, (k, ch, row[col], got)
        want = 1 if row[C_DEV2_DIG].strip() == "1" else 0
        assert rec.values[2]["d1"] == want, k


# ---------------------------------------------------------------------------
# 2. Verifikation: P=131, 2022-03-31 (CSV nur Teilzeit)
# ---------------------------------------------------------------------------
def test_second_csv_p131() -> None:
    _need(LOG_2022, CSV_2022)
    buf = LOG_2022.read_bytes()
    v = detect_variant(buf)
    day = day_from_filename(LOG_2022)
    recs = {(r.hh, r.mm, r.ss): r for r in iter_records(buf, v, day)}
    hdr, rows = read_csv(CSV_2022, day="2022-03-31")
    assert 100 <= len(rows) <= 500, len(rows)
    for row in rows:
        rec = recs[tuple(map(int, row[1].split(":")))]
        for j, ch in enumerate(f"a{i}" for i in range(1, 16)):
            want = int(round(float(row[C_DEV0_ANA[j]]) * 10))
            assert round(rec.values[0][ch] * 10) == want, (row[1], ch)
        for col, (ch, scale) in C_DEV1_ANA.items():
            want = int(round(float(row[col]) * scale))
            assert round(rec.values[1][ch] * scale) == want, (row[1], ch)
        for col, (ch, scale) in C_DEV2_ANA.items():
            want = int(round(float(row[col]) * scale))
            assert round(rec.values[2][ch] * scale) == want, (row[1], ch)


# ---------------------------------------------------------------------------
# P=137: strukturell (keine CSV-Grundwahrheit)
# ---------------------------------------------------------------------------
def test_p137_records_full_day_and_clock() -> None:
    _need(LOG_2026_P137)
    buf = LOG_2026_P137.read_bytes()
    v = detect_variant(buf)
    assert v.period == 137
    recs = list(iter_records(buf, v, "2026-03-01"))
    assert len(recs) == 8640, len(recs)
    # Uhrzeit: monoton steigend, ~10 s Grid (echte Lücken/Tick-Drift erlaubt:
    # im File sind 3 Stufen mit 8/11/12 s — kein Delta-Decoding, immer explizit).
    def secs(r: Record) -> int:
        return r.hh * 3600 + r.mm * 60 + r.ss
    dts = [secs(b) - secs(a) for a, b in zip(recs, recs[1:], strict=False)]
    assert all(dt > 0 for dt in dts), "Uhrzeiten nicht monoton"
    anomalies = [dt for dt in dts if dt != 10]
    assert len(anomalies) <= 10, anomalies
    assert secs(recs[-1]) - secs(recs[0]) >= 86000, "Tag nicht nahezu voll"
    # dev0/dev1 Felder pruefbar (Temperaturplausibilitaet)
    for r in recs:
        assert -5.0 <= r.values[0]["a8"] <= 60.0, (r.clock, r.values[0]["a8"])
        assert 5.0 <= r.values[1]["a2"] <= 35.0, (r.clock, r.values[1]["a2"])


def test_p137_widened_channels_u32() -> None:
    _need(LOG_2026_P137)
    buf = LOG_2026_P137.read_bytes()
    v = detect_variant(buf)
    off = v.data_offset
    n = (len(buf) - off) // 137
    for lo in (116, 120, 124):  # dev2 a10/a11/a12: u32 LE, Ventil 0 % / 100 %
        for k in range(n):
            raw = struct.unpack_from("<I", buf, off + 137 * k + lo)[0]
            assert raw in (0, 1000), (k, lo, raw)
    # dev2 a16 (Kollektortemperatur) signed u16, negativ erlaubt
    for k in range(n):
        val = struct.unpack_from("<h", buf, off + 137 * k + 134)[0] / 10
        assert -20.0 <= val <= 100.0, (k, val)
    # digital-Byte: nur 0 oder 0x80 (Solarpumpe)
    seen = {buf[off + 137 * k + 136] for k in range(n)}
    assert seen <= {0, 0x80}, seen


# ---------------------------------------------------------------------------
# Multi-Session / P=122
# ---------------------------------------------------------------------------
def test_multisession_split_p122_p128() -> None:
    _need(LOG_2022_MULTISESS)
    buf = LOG_2022_MULTISESS.read_bytes()
    sessions = find_sessions(buf)
    assert len(sessions) == 2, [s.variant.period for s in sessions]
    assert [s.variant.period for s in sessions] == [122, 128], [s.variant.period for s in sessions]
    # erste Session: P=122 (dev2 nur a1..a12)
    assert "a13" not in {f.ch for f in sessions[0].variant.dev2}
    assert "a12" in {f.ch for f in sessions[0].variant.dev2}
    # zweite Session: P=128 (dev2 a1..a15)
    assert "a13" in {f.ch for f in sessions[1].variant.dev2}
    # dominante Variante = P=122 (mehr Records)
    dom = detect_variant(buf)
    assert dom.period == 122, dom.period
    # beide Sessions zusammen ~1 Tag, Uhrzeit monoton
    recs = list(iter_records(buf, day=day_from_filename(LOG_2022_MULTISESS)))
    assert 8500 <= len(recs) <= 8700, len(recs)
    def secs(r: Record) -> int:
        return r.hh * 3600 + r.mm * 60 + r.ss
    # nur gueltige Uhren: Session-Grenz-Record (ueberlappende Header-Byte)
    # kann z. B. 60:60:60 enthalten und wird verworfen
    valid = [r for r in recs if r.valid_clock]
    assert len(valid) >= 8500, len(valid)
    assert all(secs(b) > secs(a) for a, b in zip(valid, valid[1:], strict=False)), "nicht monoton"
    # dev0 Felder plausibel (Vorlauf 0..90 °C)
    for r in valid:
        assert 0.0 <= r.values[0]["a6"] <= 90.0, (r.clock, r.values[0]["a6"])


# ---------------------------------------------------------------------------
# Iteration / Roestbarkeit
# ---------------------------------------------------------------------------
def test_iter_limit_and_count() -> None:
    _need(LOG_2023)
    buf = LOG_2023.read_bytes()
    v = detect_variant(buf)
    assert (len(buf) - v.data_offset) % 131 == 0
    first3 = list(iter_records(buf, v, day=day_from_filename(LOG_2023), limit=3))
    assert len(first3) == 3
    assert all(isinstance(r, Record) for r in first3)
    assert first3[0].ts_str() == "2023-01-26 00:00:03"


def test_unknown_header_raises() -> None:
    try:
        import pytest
    except ImportError:
        pytest = None
    if pytest is not None:
        with pytest.raises(ValueError):
            detect_variant(b"kein logx inhalt" * 100)
    else:
        try:
            detect_variant(b"kein logx inhalt" * 100)
        except ValueError:
            pass
        else:
            raise AssertionError("ValueError erwartet")


# ---------------------------------------------------------------------------
# standalone
# ---------------------------------------------------------------------------
def _run_all() -> int:
    fns = [
        (n, f) for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = 0
    for n, f in fns:
        try:
            f()
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {e}")
            traceback.print_exc()
        else:
            print(f"ok   {n}")
    total = len(fns)
    print(f"\n{total - failed}/{total} tests bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
