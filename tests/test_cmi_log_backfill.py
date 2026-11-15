"""Tests für analysis/cmi_log_backfill.py (Q-121 Phase 2: host-agnostisch).

Abdeckung:
* dynamische File-Erkennung (raw-dir glob, nur ``data_*.log``-Basenames)
* wachsende Files ohne Größen-Erwartung laufen durch (kein Abort);
  ``--expected-size`` = harter Check (Mismatch = Abort)
* TS-Safety-Guard: Store-ts außerhalb der Quelle -> Abort (Exit != 0),
  Store unverändert (kein .part-Rest, kein Write)
* ``--months`` begrenzt den Rewrite auf die gewählten Monate
* (zusätzlich) malformierte ``--months``/``--expected-size`` = Abort
* fehlender raw-dir / fehlende Data-Files -> Abort

Hinweis: Das echte LogX-Gap-File (``discovery/cmi-log/logx_...``) liegt im
Repo, daher erzeugen echte Runs neben dem Roh-monaten zusätzlich 2026-10.
Die Tests setzen die Store-/DB-Pfade auf tmp und begrenzen die Writes
meist über ``--months 2026-11``.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import duckdb
import pytest

import analysis.cmi_log_backfill as bf
from app.cmi.cmi_log import RECORD_PERIOD
from app.cmi.cmi_log_pq import WIDE_COLUMNS, CmiLogParquet

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAGIC = b"8D312121\r\n"


# ---------------------------------------------------------------------------
# synthetic CMI /LOG files (gleicher Minimalstil wie tests/test_cmi_log.py)
# ---------------------------------------------------------------------------
def _record(hh: int, mm: int, ss: int, slots: dict[int, int] | None = None) -> bytes:
    """One synthetic P=169 record: valid time + optional explicit u16 slots."""
    rec = bytearray(RECORD_PERIOD)
    for slot, raw in (slots or {}).items():
        struct.pack_into("<H", rec, 2 * slot, raw)
    rec[108] = hh
    rec[107] = mm
    rec[106] = ss
    return bytes(rec)


def _advance(hh: int, mm: int, ss: int) -> tuple[int, int, int]:
    total = hh * 3600 + mm * 60 + ss + 10
    return total // 3600, (total % 3600) // 60, total % 60


def make_log_file(
    name: str,
    start: tuple[int, int, int],
    n_records: int = 6,
    slots: dict[int, int] | None = None,
) -> tuple[str, bytes]:
    """Minimal valid CMI /LOG data file (Magic + one 0x43 header block +
    ``n_records`` P-grid records; record 0 straddles the header -> the decoder
    skips it, so ``n_records - 1`` records are decoded, ts 00:00:11 ff.)."""
    parts = [_MAGIC, b"\x43" * (RECORD_PERIOD - 10)]
    hh, mm, ss = start
    for _ in range(n_records):
        parts.append(_record(hh, mm, ss, slots))
        hh, mm, ss = _advance(hh, mm, ss)
    return name, b"".join(parts)


def _wide_row(value: float = 1.0) -> dict[str, float]:
    """Ein komplett gefüllter 70-Spalten-Wide-Row (Store-Seed-Zeile)."""
    return {c: value for c in WIDE_COLUMNS[1:]}


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Backfill-Umfeld auf tmp-Pfade (kein reales Store-/DB-File)."""
    store_dir = tmp_path / "cmi_log_pq"
    db = tmp_path / "history.db"
    monkeypatch.setenv("CMI_LOG_PARQUET_DIR", str(store_dir))
    monkeypatch.setenv("HISTORY_DB_PATH", str(db))
    return {"store": store_dir, "db": db, "tmp": tmp_path}


@pytest.fixture
def needs_winsol_ref() -> None:
    """Real-Backfill-Runs (kein Dry-Run) scheitern fail-closed ohne die
    ``winsol_pq``-Schema-Referenz (ein Laufzeit-Datenfile, das nie versioniert
    wird). Im Dev-Tree existiert sie und die Tests laufen voll; im Public-Repo
    werden sie übersprungen."""
    ref = REPO_ROOT / "data" / "winsol_pq" / "2026" / "10" / "2026-10.parquet"
    if not ref.is_file():
        pytest.skip("winsol_pq-Referenz nicht vorhanden (public repo)")


# ---------------------------------------------------------------------------
# 1. dynamische File-Erkennung
# ---------------------------------------------------------------------------
def test_globs_raw_dir_ignores_non_data_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Nur Basenames ``data_<JJJJ>_<MM>_<DD>_<HH>_<MM>_<SS>.log`` werden
    dekodiert; README.txt / x.log / unvollständige data_*.log bleiben draußen."""
    raw = tmp_path / "raw"
    raw.mkdir()
    name_a, body_a = make_log_file("data_2026_11_01_00_00_01.log", (0, 0, 1))
    name_b, body_b = make_log_file("data_2026_11_02_00_00_01.log", (0, 0, 1))
    (raw / name_a).write_bytes(body_a)
    (raw / name_b).write_bytes(body_b)
    (raw / "README.txt").write_text("kein /LOG-File\n")
    (raw / "x.log").write_bytes(body_a)  # falscher Basenames -> ignorieren
    (raw / "data_2026_11_03.log").write_bytes(body_a)  # kein Zeitstempel -> ignorieren

    found = bf.discover_raw_files(raw)
    assert [p.name for p in found] == [name_a, name_b]

    assert bf.main(["--dry-run", "--raw-dir", str(raw)]) == 0
    out = capsys.readouterr().out
    assert "raw-dir:" in out
    assert name_a in out and name_b in out
    assert "x.log" not in out
    assert "README.txt" not in out
    assert "data_2026_11_03.log" not in out


# ---------------------------------------------------------------------------
# 2. wachsende Files: kein Größen-Abort ohne --expected-size
# ---------------------------------------------------------------------------
def test_growing_file_no_size_abort(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], needs_winsol_ref: None
) -> None:
    """File ohne Größen-Erwartung (auch nicht im Rosetta-25-Set) -> läuft
    durch (echter Lauf, kein Abort).  Signed-Decoder wirkt end-to-end
    (Slot 46 = dev2 a16, signed i16 /10: raw 0xFFF7 = -9 -> -0.9)."""
    raw = env["tmp"] / "raw"
    # 0xFFF7 = -9 (i16) -> dev2 a16 = -0.9 °C (signed, Q-121)
    name, body = make_log_file(
        "data_2026_11_01_00_00_01.log", (0, 0, 1), n_records=6,
        slots={46: 0xFFF7},
    )
    raw.mkdir(parents=True)
    (raw / name).write_bytes(body)

    assert bf.main(["--raw-dir", str(raw), "--months", "2026-11"]) == 0
    out = capsys.readouterr().out
    assert "WARNUNG (Rosetta-25" not in out
    assert "ABBRUCH" not in out

    mfile = env["store"] / "2026" / "11" / "2026-11.parquet"
    assert mfile.exists()
    con = duckdb.connect()
    try:
        n, mn = con.execute(
            f"SELECT COUNT(*), MIN(dev2_analog_16) FROM read_parquet('{mfile}')"
        ).fetchone()
    finally:
        con.close()
    assert n == 5  # record 0 (Header-Overlap) wird vom Decoder verworfen
    assert mn == pytest.approx(-0.9)  # signed-Decoder durch den Backfill-Pfad


def test_expected_size_mismatch_aborts(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], needs_winsol_ref: None
) -> None:
    """--expected-size (harter Check): Mismatch -> Abort rc=1, kein Write;
    Treffer -> läuft durch."""
    raw = env["tmp"] / "raw"
    name, body = make_log_file("data_2026_11_01_00_00_01.log", (0, 0, 1))
    raw.mkdir(parents=True)
    (raw / name).write_bytes(body)

    rc = bf.main(["--raw-dir", str(raw), "--months", "2026-11",
                  "--expected-size", f"{name}={len(body) + 1}"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ABBRUCH (expected-size)" in err and name in err
    assert not (env["store"] / "2026" / "11" / "2026-11.parquet").exists()

    rc = bf.main(["--raw-dir", str(raw), "--months", "2026-11",
                  "--expected-size", f"{name}={len(body)}"])
    assert rc == 0
    assert (env["store"] / "2026" / "11" / "2026-11.parquet").exists()


# ---------------------------------------------------------------------------
# 3. TS-Safety-Guard (Pi-Partial-Set-Schutz)
# ---------------------------------------------------------------------------
def test_ts_safety_guard_aborts_on_missing_day(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Store-ts, das in KEINER Quelldatei vorkommt (Tag fehlt im raw-dir)
    -> Abort (rc != 0 + klare Meldung), Store byte-identisch, kein .part-Rest."""
    store: Path = env["store"]
    seed = CmiLogParquet(store)
    # Store: ein ts IN der Quelle + zwei ts, die in keiner Rohdatei liegen.
    seed.write_month("2026-11", [
        ("2026-11-01 00:00:11", _wide_row()),
        ("2026-11-01 00:00:00", _wide_row()),   # fehlt in der Quelle
        ("2026-11-05 12:00:00", _wide_row()),   # ganzer Tag fehlt im raw-dir
    ])
    mfile = store / "2026" / "11" / "2026-11.parquet"
    sha_before = _sha256(mfile)
    mtime_before = mfile.stat().st_mtime_ns
    assert mfile.exists()

    raw = env["tmp"] / "raw"
    name, body = make_log_file("data_2026_11_01_00_00_01.log", (0, 0, 1))
    raw.mkdir(parents=True)
    (raw / name).write_bytes(body)  # liefert 00:00:11 .. 00:00:51 (5 Records)

    rc = bf.main(["--raw-dir", str(raw), "--months", "2026-11"])
    assert rc != 0
    r = capsys.readouterr()
    out = r.out + r.err
    assert "ABBRUCH (TS-Safety 2026-11 fehlgeschlagen)" in out
    assert "Store-ts fehlen in der Quelle" in out
    assert "2026-11-05 12:00:00" in out  # fehlender ts in der Meldung

    # Store unverändert: byte-identisch, mtime identisch, kein .part-Rest.
    assert _sha256(mfile) == sha_before
    assert mfile.stat().st_mtime_ns == mtime_before
    assert not list(store.rglob("*.part"))


# ---------------------------------------------------------------------------
# 4. --months begrenzt den Rewrite
# ---------------------------------------------------------------------------
def test_months_arg_limits_rewrite(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], needs_winsol_ref: None
) -> None:
    """--months 2026-11: nur 2026-11 wird (neu) geschrieben; die 2026-10-
    Datei bleibt mtime- + byte-identisch (nicht in den Quelldateien abgedeckt
    -> wird übersprungen, nicht geprüft, nicht written)."""
    store: Path = env["store"]
    seed = CmiLogParquet(store)
    seed.write_month("2026-10", [("2026-10-15 00:00:00", _wide_row())])
    seed.write_month("2026-11", [("2026-11-01 00:00:11", _wide_row())])
    oct_file = store / "2026" / "10" / "2026-10.parquet"
    nov_file = store / "2026" / "11" / "2026-11.parquet"
    sha_oct_before = _sha256(oct_file)
    mtime_oct_before = oct_file.stat().st_mtime_ns

    raw = env["tmp"] / "raw"
    name, body = make_log_file("data_2026_11_01_00_00_01.log", (0, 0, 1))
    raw.mkdir(parents=True)
    (raw / name).write_bytes(body)  # liefert 00:00:11..00:00:51 -> 4 neue ts

    rc = bf.main(["--raw-dir", str(raw), "--months", "2026-11"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TS-Safety 2026-11: OK" in out
    assert "parquet: month=2026-11" in out
    assert "month=2026-10" not in out  # Oktober wurde NICHT geschrieben

    con = duckdb.connect()
    try:
        ts = {t[0] for t in con.execute(f"SELECT ts FROM read_parquet('{nov_file}')").fetchall()}
        n10, _ = con.execute(f"SELECT COUNT(*), MIN(ts) FROM read_parquet('{oct_file}')").fetchone()
    finally:
        con.close()
    assert ts == {f"2026-11-01 00:00:{s:02d}" for s in (11, 21, 31, 41, 51)}
    assert n10 == 1  # Oktober-Inhalt unverändert

    # Oktober: unverändert (mtime + byte-identisch)
    assert oct_file.stat().st_mtime_ns == mtime_oct_before
    assert _sha256(oct_file) == sha_oct_before
    assert not list(store.rglob("*.part"))


def test_months_not_covered_by_source_aborts_cleanly(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], needs_winsol_ref: None
) -> None:
    """--months-Monat, den weder Rohdateien noch die LogX-Gap abdecken ->
    Hinweis + sauberes Exit 1 (nichts zu schreiben), kein Write,
    bestehende Monate intakt. (2025-12: weder in den Raw- noch in den
    LogX-Daten.)"""
    store: Path = env["store"]
    seed = CmiLogParquet(store)
    seed.write_month("2026-11", [("2026-11-01 00:00:11", _wide_row())])
    mfile = store / "2026" / "11" / "2026-11.parquet"
    sha_before = _sha256(mfile)

    raw = env["tmp"] / "raw"
    raw.mkdir(parents=True)
    name, body = make_log_file("data_2026_11_02_00_00_01.log", (0, 0, 1))
    (raw / name).write_bytes(body)

    rc = bf.main(["--raw-dir", str(raw), "--months", "2025-12"])
    assert rc == 1
    r = capsys.readouterr()
    out = r.out + r.err
    assert "wird von keinen Quelldateien abgedeckt" in out
    assert "ABBRUCH (nichts zu schreiben)" in out
    assert _sha256(mfile) == sha_before  # 2026-11 bleibt unverändert


# ---------------------------------------------------------------------------
# 5. CLI-Validierung
# ---------------------------------------------------------------------------
def test_malformed_months_and_expected_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformierte --months / --expected-size -> SystemExit (klare Meldung)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "data_2026_11_01_00_00_01.log").write_bytes(b"\x43")  # vorhanden, damit
    # der raw-dir-Check durchläuft und die (mal)formierte-Argument-Prüfung greift
    monkeypatch.setenv("CMI_LOG_RAW_DIR", str(raw))
    with pytest.raises(SystemExit) as exc:
        bf.main(["--months", "2026-1"])
    assert "malformierter Monat" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        bf.main(["--expected-size", "data_2026_11_01_00_00_01.log=abc"])
    assert "malformierter Eintrag" in str(exc.value)


def test_missing_raw_dir_aborts(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """raw-dir existiert nicht / enthält keine Data-Files -> Abort rc=2."""
    assert bf.main(["--raw-dir", str(env["tmp"] / "does-not-exist")]) == 2
    empty = env["tmp"] / "empty"
    empty.mkdir()
    (empty / "README.txt").write_text("leer")
    assert bf.main(["--raw-dir", str(empty)]) == 2
    assert not list(env["store"].rglob("*.parquet"))
