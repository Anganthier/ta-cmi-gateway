"""Tests für analysis/oil_burner_kpi.py (ÖL-KPI-1).

Synthetic-Fixtures (kleine Parquet-Files im pytest-Temp-Dir) für die
deterministische Abdeckung von:
* T_ign-Schwelle (Zünd-Rampe, Nachglühen, Kalt-Start)
* 2-Stufen-Ausfall-Regel (strikt / intermittierend / Kandidaten-Schwelle)
* Ausfall-Fenster-Gruppierung (inkl. 1-Tag-Bridge)
* Q-127 Tank-Modell: Anker-Segmente S1/S2/S3/S3a/S3b (Voll-Anker 4500 L,
  KPI-Kanon ab 01.06.2023), Historie-Segmente (usable=False),
  old_burner_baseline, A1/A2 gelöst, Sub-Segment-Filter
* Monats-/Saison-Aggregationen, Trend-Fit, Monats-CSV, Era-Szenarien
* v0.6-D Kompass-Metrik: L/Heizgradtag (HDD-15) roh + holz-korrigiert
  (Stunden-Proxy, O6-Exklusion) je Segment/Saison/Monat, Gauge-Band,
  Holz-primär-Tage, degenerierte Sommer-Monate (MIN_MONTH_HDD15)

Real-Data-Smoke-Tests (``@pytest.mark.slow``, im Standard-Run NICHT aktiv):
* Januar-2026-Ausfall-Fenster 14.01.–29.01. wird erkannt
* Anker-Segmente, Baseline, Era-Szenarien und Saisons n=4 (Werte aus dem
  echten CLI-Run oel_kpi.json abgelesen - NICHT geraten)
* v0.6-D-Kernzahlen (S1–S3b, Saisons, Okt-2026) aus dem CLI-Run
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import analysis.oil_burner_kpi as kpi

SAMPLE_SECONDS = 10  # synthetic grid: the 10 s grid the module assumes
SAMPLES_PER_DAY = 86400 // SAMPLE_SECONDS  # 8640
SAMPLES_PER_HOUR = 3600 // SAMPLE_SECONDS  # 360

REAL_START = "2020-09-01"
REAL_END = "2026-11-10"


def _require_real_data() -> None:
    """Real-Data-Tests (``-m slow``) brauchen die Parquet-Stores unter
    ``data/`` (winsol_pq + cmi_log_pq) und die Tank-CSV -- im Public-Repo
    (``data/`` ist .gitignore'd) nicht vorhanden -> skip."""
    root = Path(__file__).resolve().parent.parent
    if not (root / "data" / "winsol_pq").is_dir():
        pytest.skip("Real-Data-Parquet-Stores nicht vorhanden (public repo)")


# ---------------------------------------------------------------------------
# synthetic timeseries helpers
# ---------------------------------------------------------------------------
def make_daily_ts(day: str, demand: list[int], supply: list[float]) -> list[dict[str, float]]:
    """Build one day of synthetic samples on the 10 s grid.

    ``demand`` / ``supply`` are constant-value patterns (same length).
    """
    assert len(demand) == len(supply) == SAMPLES_PER_DAY
    ts = [
        f"{day} {i // 360:02d}:{(i % 360) // 6:02d}:{(i % 6) * 10:02d}"
        for i in range(SAMPLES_PER_DAY)
    ]
    out: list[dict[str, float]] = []
    for t, d, s in zip(ts, demand, supply, strict=True):
        out.append(
            {
                "ts": t,
                "dev0_digital_05": float(d),
                "dev0_analog_05": s,
                "dev0_analog_08": 5.0,
                "dev0_analog_09": 50.0,
                "dev0_digital_06": 0.0,
                "dev1_analog_01": 0.0,
            }
        )
    return out


def normal_day(day: str) -> list[dict[str, float]]:
    """Normal heating day: demand in two 6 h blocks; supply of block 1 ramps
    25 -> 75 deg C (crossing T_ign after 216 samples) then stays warm."""
    n = SAMPLES_PER_DAY
    block = 6 * SAMPLES_PER_HOUR  # 2160
    demand = [1] * block + [0] * (n - 2 * block) + [1] * block
    supply = (
        [25.0 + 50.0 * i / block for i in range(block)] + [75.0] * (n - 2 * block) + [75.0] * block
    )
    return make_daily_ts(day, demand, supply)


def failure_day(day: str) -> list[dict[str, float]]:
    """Failure day: 10 h demand, supply stuck at 20 deg C (ignition never
    succeeds) -> 10 h cold demand, a5 max 20 deg C -> STRICT failure day."""
    n = SAMPLES_PER_DAY
    demand = [1] * (10 * SAMPLES_PER_HOUR) + [0] * (n - 10 * SAMPLES_PER_HOUR)
    supply = [20.0] * n
    return make_daily_ts(day, demand, supply)


def intermittent_day(day: str) -> list[dict[str, float]]:
    """Intermittent failure day: 10 h demand, supply mostly 20 deg C but one
    lucky ignition to 65 deg C (a5 max >= 40) -> NOT excluded."""
    n = SAMPLES_PER_DAY
    demand = [1] * (10 * SAMPLES_PER_HOUR) + [0] * (n - 10 * SAMPLES_PER_HOUR)
    supply = [20.0] * n
    supply[3500:3512] = [65.0] * 12
    return make_daily_ts(day, demand, supply)


def ramp_day(day: str) -> list[dict[str, float]]:
    """Ignition-ramp day: demand 8 h, supply 25 deg C for the first 4 h then
    80 deg C (ramp crossing T_ign mid-block; a5 max 80 > 78 so the day stays
    out of the failure rule and purely tests the T_ign boundary)."""
    n = SAMPLES_PER_DAY
    demand = [1] * (8 * SAMPLES_PER_HOUR) + [0] * (n - 8 * SAMPLES_PER_HOUR)
    supply = [25.0] * (4 * SAMPLES_PER_HOUR) + [80.0] * (n - 4 * SAMPLES_PER_HOUR)
    return make_daily_ts(day, demand, supply)


def glow_day(day: str) -> list[dict[str, float]]:
    """Afterglow day: burner OFF all day, supply at 60 deg C -> raw hours
    must NOT count, effective hours = 0."""
    n = SAMPLES_PER_DAY
    demand = [0] * n
    supply = [60.0] * n
    return make_daily_ts(day, demand, supply)


def write_parquet(base: Path, day: str, rows: list[dict[str, float]]) -> None:
    """Write one day of rows to the Hive layout YYYY/MM/YYYY-MM.parquet
    (appending to the monthly file when other days of the month exist)."""
    y, m = day[:4], day[5:7]
    path = base / y / m / f"{y}-{m}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({k: [r[k] for r in rows] for k in rows[0]})
    if path.exists():
        table = pa.concat_tables([pq.read_table(str(path)), table])
    pq.write_table(table, str(path))


def synthetic_timeseries(winsol: Path, cmi: Path) -> None:
    """Write the canonical synthetic window split across both Parquet sources
    (mirrors the real layout: winsol_pq = canonical history, cmi_log_pq =
    continuation).  2030 days -> ``winsol``, 2031 days -> ``cmi``; both globs
    must match at least one file (DuckDB read_parquet list quirk)."""
    days_2030 = [
        normal_day("2030-01-10"),
        normal_day("2030-01-11"),
    ]
    days_2030 += [failure_day(f"2030-01-{d:02d}") for d in (14, 15, 16, 17)]
    days_2030 += [
        intermittent_day("2030-01-18"),
        ramp_day("2030-01-19"),
        glow_day("2030-01-20"),
        normal_day("2030-02-06"),
        normal_day("2030-02-07"),
    ]
    days_2031 = [
        normal_day("2031-01-05"),
        normal_day("2031-01-06"),
        normal_day("2031-01-07"),
        normal_day("2031-01-08"),
    ]
    for rows in days_2030:
        write_parquet(winsol, rows[0]["ts"][:10], rows)
    for rows in days_2031:
        write_parquet(cmi, rows[0]["ts"][:10], rows)


# ---------------------------------------------------------------------------
# synthetic tank CSV + Q-127 anchor model
# ---------------------------------------------------------------------------
def write_tank_csv(path: Path) -> None:
    """Synthetic tank-gauge CSV in the exact real-file format (``#`` comment
    block, no header row, 7- or 8-field rows).

    Reading plan (data window 2030-01-10 .. 2031-01-08; anchor model:
    TANK_VALID_FROM = 2030-01-05, FULL = 1000 L, GAUGE = 30 L):
    * 2030-01-02 (delivery row, documentary full delivery)
    * 2030-01-20 / 2030-02-01 / 2030-03-01 / 2030-03-15: gauge rows ->
      the pairs form the HISTORIE segments of the Q-127 model
      (all usable=False: pre-TANK_VALID_FROM rows / outside the anchor
      canon; the 03-01 -> 03-15 pair is a zero-decrease row).
    The KPI segment canon itself is NOT the CSV but the patched
    KPI_ANCHOR_SEGMENTS fixture (S1/S2/S3/S3a/S3b in the 2030/2031 window).
    """
    lines = [
        "# synthetic tank test",
        "# datum;hoehe_mm;ltr;fehlmeng;getankt;menge_l;summe_eur;ltr_pro_tag_owner",
        "# --- Verwertbare Verbrauch-Segmente (synthetic) ---",
        "# 2030-01-02->2030-01-20:  60 L / 18 d (HISTORIE)",
        "2030-01-02;1500;4500;300;WAHR;4000;1000.00",
        "2030-01-20;1480;4440;360;;;",
        "2030-02-01;1500;4500;300;WAHR;600;200.00",
        "2030-03-01;1650;4650;150;;",
        "2030-03-15;1650;4650;150;;",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _synthetic_anchor_segments() -> tuple:
    """Q-127 KPI anchor canon relocated to the 2030/2031 test window.

    S1/S2 are exact full-to-full (1000 L anchors); S3 and its sub-segments
    S3a/S3b carry the 30 L gauge uncertainty (TANK_GAUGE_UNCERTAINTY_L is
    patched to 30 L in the fixture).
    """
    return (
        ("S1", "2030-01-05", "2030-03-01", 400.0, True, "synthetic S1, exakt"),
        ("S2", "2030-03-01", "2030-06-01", 350.0, True, "synthetic S2, exakt"),
        ("S3", "2030-06-01", "2031-01-08", 260.0, False, "synthetic S3, grob"),
        ("S3a", "2030-06-01", "2030-09-01", 200.0, False, "synthetic S3a"),
        ("S3b", "2030-09-01", "2031-01-08", 60.0, False, "synthetic S3b / mini"),
    )


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Repo-root layout with synthetic Parquet + tank CSV under tmp_path,
    plus the Q-127 anchor model patched into the 2030/2031 test window."""
    root = tmp_path / "cmi"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(kpi, "REPO_ROOT", root)
    data_dir = root / "data"
    synthetic_timeseries(data_dir / "winsol_pq", data_dir / "cmi_log_pq")
    globs = (
        f"{data_dir}/winsol_pq/*/*/*.parquet",
        f"{data_dir}/cmi_log_pq/*/*/*.parquet",
    )
    monkeypatch.setattr(kpi, "DEFAULT_PARQUET_GLOBS", globs)
    # Q-127 anchor model patched into the synthetic window: the KPI segment
    # canon is an explicit constant, not the CSV gauge sequence.
    monkeypatch.setattr(kpi, "TANK_VALID_FROM", "2030-01-05")
    monkeypatch.setattr(kpi, "TANK_FULL_L", 1000.0)
    monkeypatch.setattr(kpi, "TANK_GAUGE_UNCERTAINTY_L", 30.0)
    monkeypatch.setattr(kpi, "KPI_ANCHOR_SEGMENTS", _synthetic_anchor_segments())
    monkeypatch.setattr(kpi, "OLD_BURNER_SEGMENT_LABELS", ("S1", "S2"))
    monkeypatch.setattr(kpi, "MINI_SEGMENT_START", "2030-09-01")
    monkeypatch.setattr(kpi, "MINI_SEGMENT_END", "2031-01-08")
    monkeypatch.setattr(kpi, "S3_SPLIT_DATE", "2030-09-01")
    tank = root / "tmp" / "tank_test.csv"
    tank.parent.mkdir(parents=True, exist_ok=True)
    write_tank_csv(tank)
    return {
        "root": root,
        "tank": tank,
        "out": root / "tmp" / "oel_kpi",
        "start": "2030-01-10",
        "end": "2031-01-08",
    }


def run_analysis(synthetic: dict[str, object]) -> dict[str, object]:
    """Run the full analysis on the synthetic data and return the payload."""
    out = Path(str(synthetic["out"]))
    payload = kpi.run(
        str(synthetic["start"]), str(synthetic["end"]), 30.0, out, Path(str(synthetic["tank"]))
    )
    assert (out / "oel_kpi.json").exists()
    assert (out / "burn_hours_monthly.csv").exists()
    assert (out / "report.md").exists()
    # disk JSON and in-memory payload agree on the totals
    disk = json.loads((out / "oel_kpi.json").read_text(encoding="utf-8"))
    assert disk["totals"]["raw_hours"] == payload["totals"]["raw_hours"]
    return payload


# ---------------------------------------------------------------------------
# date helpers
# ---------------------------------------------------------------------------
def test_season_id_for() -> None:
    assert kpi.season_id_for(date(2020, 10, 9)) == (2020, 2021)
    assert kpi.season_id_for(date(2021, 4, 30)) == (2020, 2021)
    assert kpi.season_id_for(date(2021, 5, 1)) is None
    assert kpi.season_id_for(date(2021, 9, 30)) is None
    assert kpi.season_id_for(date(2022, 1, 1)) == (2021, 2022)


def test_summer_season_for() -> None:
    assert kpi.summer_season_for(date(2020, 7, 15)) == (2020, 2021)
    assert kpi.summer_season_for(date(2020, 10, 1)) is None
    assert kpi.summer_season_for(date(2020, 4, 1)) is None


def test_days_in_month_december() -> None:
    assert kpi.days_in_month(2024, 12) == 31
    assert kpi.days_in_month(2024, 2) == 29
    assert kpi.days_in_month(2025, 2) == 28


def test_daily_hdd15() -> None:
    assert kpi.daily_hdd15(10.0) == 5.0
    assert kpi.daily_hdd15(20.0) == 0.0


def test_clamp_to_window() -> None:
    assert kpi.clamp_to_window(
        date(2020, 1, 1), date(2020, 12, 31), date(2020, 9, 1), date(2020, 12, 31)
    ) == (
        date(2020, 9, 1),
        date(2020, 12, 31),
    )
    assert (
        kpi.clamp_to_window(
            date(2019, 1, 1), date(2019, 12, 31), date(2020, 9, 1), date(2020, 12, 31)
        )
        is None
    )


# ---------------------------------------------------------------------------
# tank CSV reading
# ---------------------------------------------------------------------------
def test_read_tank_csv(tmp_path: Path) -> None:
    p = tmp_path / "tank.csv"
    write_tank_csv(p)
    meas = kpi.read_tank_csv(p)
    assert [m.day for m in meas] == [
        "2030-01-02",
        "2030-01-20",
        "2030-02-01",
        "2030-03-01",
        "2030-03-15",
    ]
    assert meas[0].delivery is True and meas[0].delivery_qty == 4000.0
    assert meas[1].delivery is False and meas[1].delivery_qty is None
    assert meas[2].delivery is True and meas[2].delivery_qty == 600.0
    assert meas[3].litres == 4650.0


def test_read_tank_csv_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("# c\n2030-01-02;1500;4500\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected row"):
        kpi.read_tank_csv(p)


# ---------------------------------------------------------------------------
# daily loading: T_ign, ramp, afterglow
# ---------------------------------------------------------------------------
def test_load_daily_synthetic(synthetic: dict[str, object]) -> None:
    globs = kpi.DEFAULT_PARQUET_GLOBS
    con = kpi.make_connection()
    try:
        daily = kpi.load_daily(con, globs, str(synthetic["start"]), str(synthetic["end"]), 30.0)
    finally:
        con.close()
    by_day = {d.day: d for d in daily}
    assert len(daily) == 15

    # normal day: 12 h demand (two 6 h blocks); block-1 supply ramps
    # 25 -> 75 deg C and crosses T_ign after 216 samples -> 4104 eff samples
    n = by_day["2030-01-10"]
    assert n.n_samples == SAMPLES_PER_DAY
    assert n.raw_hours == pytest.approx(12.0)
    assert n.eff_hours == pytest.approx((2160 - 216 + 2160) / SAMPLES_PER_HOUR)
    assert n.cold30_hours == pytest.approx(216 / SAMPLES_PER_HOUR)

    # failure day: 10 h demand, all cold (a5 stuck at 20 deg C)
    f = by_day["2030-01-14"]
    assert f.raw_hours == pytest.approx(10.0)
    assert f.eff_hours == 0.0
    assert f.cold30_hours == pytest.approx(10.0)
    assert f.a5_max == pytest.approx(20.0)

    # intermittent day: a5 max 65 -> 12 warm samples count as burned
    i = by_day["2030-01-18"]
    assert i.a5_max == pytest.approx(65.0)
    assert i.eff_hours == pytest.approx(12 / SAMPLES_PER_HOUR)

    # ramp day: 8 h demand, first 4 h cold (25 deg C), rest warm (80 deg C)
    r = by_day["2030-01-19"]
    assert r.cold30_hours == pytest.approx(4.0)
    assert r.eff_hours == pytest.approx(4.0)
    assert r.a5_max == pytest.approx(80.0)

    # afterglow day: demand OFF -> raw = eff = 0, cold = 0
    g = by_day["2030-01-20"]
    assert g.raw_hours == 0.0
    assert g.eff_hours == 0.0
    assert g.a5_avg == pytest.approx(60.0)


def test_sensitivity_synthetic(synthetic: dict[str, object]) -> None:
    con = kpi.make_connection()
    try:
        sens = kpi.load_sensitivity(
            con,
            kpi.DEFAULT_PARQUET_GLOBS,
            str(synthetic["start"]),
            str(synthetic["end"]),
        )
    finally:
        con.close()
    assert set(sens) == {28.0, 30.0, 35.0}
    # T=35 excludes the 30-35 ramp samples, T=28 includes more
    assert sens[35.0] < sens[30.0] < sens[28.0]


# ---------------------------------------------------------------------------
# failure rule (two tiers) + windows
# ---------------------------------------------------------------------------
def _synthetic_daily() -> list[kpi.DayAgg]:
    """Small synthetic daily aggregate set covering all rule branches."""
    base = dict(
        n_samples=100,
        raw_hours=5.0,
        eff_hours=0.0,
        a5_avg=20.0,
        ext_avg=5.0,
        wood_hours=0.0,
        o6_hours=0.0,
        is_failure=False,
    )
    return [
        kpi.DayAgg(day="2030-01-13", cold30_hours=2.4, a5_max=30.0, **base),
        kpi.DayAgg(day="2030-01-14", cold30_hours=5.0, a5_max=90.0, **base),
        kpi.DayAgg(day="2030-01-15", cold30_hours=5.0, a5_max=30.0, **base),
        kpi.DayAgg(day="2030-01-16", cold30_hours=3.0, a5_max=30.0, **base),
        kpi.DayAgg(day="2030-01-18", cold30_hours=4.0, a5_max=60.0, **base),
        kpi.DayAgg(day="2030-01-19", cold30_hours=2.6, a5_max=35.0, **base),
        kpi.DayAgg(day="2030-01-22", cold30_hours=4.0, a5_max=25.0, **base),
    ]


def test_detect_failure_days_two_tiers() -> None:
    failures = kpi.detect_failure_days(_synthetic_daily())
    by_day = {f.day: f.kind for f in failures}
    assert by_day == {
        "2030-01-15": "strict",  # >= 4 h cold, a5 max < 40
        "2030-01-16": "intermittent",  # 3.0 h < 4 h strict floor
        "2030-01-18": "intermittent",  # a5 max in [40, 78]
        "2030-01-19": "intermittent",  # 2.6 h >= 2.5 h candidate floor
        "2030-01-22": "strict",  # 4.0 h boundary, a5 max < 40
    }
    # 01-13 below candidate floor, 01-14 above the a5 cap -> not candidates
    assert "2030-01-13" not in by_day
    assert "2030-01-14" not in by_day


def test_group_failure_windows() -> None:
    failures = kpi.detect_failure_days(_synthetic_daily())
    windows = kpi.group_failure_windows(failures)
    assert len(windows) == 2
    w1, w2 = windows
    assert (w1.start, w1.end) == ("2030-01-15", "2030-01-19")
    assert w1.candidate_days == 4 and w1.strict_days == 1 and w1.intermittent_days == 3
    assert w1.gap_days == 1  # 01-17 sits inside the window (1-day bridge)
    assert (w2.start, w2.end) == ("2030-01-22", "2030-01-22")
    assert w2.strict_days == 1
    # empty input
    assert kpi.group_failure_windows([]) == []


def test_group_failure_windows_no_strict_is_dropped() -> None:
    failures = [
        kpi.FailureDay("2030-01-16", "intermittent", 5.0, 2.0, 60.0, 30.0, 5.0),
        kpi.FailureDay("2030-01-17", "intermittent", 5.0, 2.0, 60.0, 30.0, 5.0),
    ]
    assert kpi.group_failure_windows(failures) == []


def test_mark_failure_days_strict_only() -> None:
    daily = _synthetic_daily()
    failures = kpi.detect_failure_days(daily)
    kpi.mark_failure_days(daily, failures)
    marked = {d.day for d in daily if d.is_failure}
    assert marked == {"2030-01-15", "2030-01-22"}  # strict days only


# ---------------------------------------------------------------------------
# Oct-2025 maintainer-Override
# ---------------------------------------------------------------------------
_OVR_BASE = dict(
    n_samples=100,
    raw_hours=24.0,
    eff_hours=0.0,
    a5_avg=21.0,
    ext_avg=10.0,
    wood_hours=0.0,
    o6_hours=0.0,
)


def _override_daily() -> list[kpi.DayAgg]:
    return [
        kpi.DayAgg(day="2025-10-13", cold30_hours=24.0, a5_max=22.2, **_OVR_BASE),
        kpi.DayAgg(day="2025-10-14", cold30_hours=24.0, a5_max=21.9, **_OVR_BASE),
        kpi.DayAgg(day="2026-01-15", cold30_hours=21.9, a5_max=24.4, **_OVR_BASE),
    ]


def test_detect_failure_days_oct2025_override() -> None:
    on = {f.day: f for f in kpi.detect_failure_days(_override_daily(), apply_oct2025_override=True)}
    assert on["2025-10-13"].kind == "intermittent"
    assert on["2025-10-13"].overridden is True
    assert on["2025-10-14"].kind == "intermittent"
    assert on["2026-01-15"].kind == "strict"  # NOT in the override set
    assert on["2026-01-15"].overridden is False
    off = {
        f.day: f for f in kpi.detect_failure_days(_override_daily(), apply_oct2025_override=False)
    }
    assert off["2025-10-13"].kind == "strict"
    assert off["2025-10-13"].overridden is False
    assert off["2026-01-15"].kind == "strict"


def test_group_failure_windows_override_note() -> None:
    failures = kpi.detect_failure_days(_override_daily(), apply_oct2025_override=True)
    windows = kpi.group_failure_windows(failures)
    assert len(windows) == 2
    w_oct, w_jan = windows
    assert (w_oct.start, w_oct.end) == ("2025-10-13", "2025-10-14")
    assert w_oct.strict_days == 0
    assert w_oct.intermittent_days == 2
    assert w_oct.overridden_days == 2
    assert w_oct.excluded_cold_hours == 0.0
    assert "maintainer-Override" in w_oct.note
    # Jan-2026 stays the strict window
    assert (w_jan.start, w_jan.end) == ("2026-01-15", "2026-01-15")
    assert w_jan.strict_days == 1 and w_jan.overridden_days == 0
    assert w_jan.note == ""


# ---------------------------------------------------------------------------
# tank segments (Q-127 anchor model)
# ---------------------------------------------------------------------------
def test_build_anchor_segments() -> None:
    """The KPI canon: S1/S2 exact, S3 + sub-segments gauge-flagged."""
    segments = kpi.build_anchor_segments("2020-09-01", "2026-11-10")
    by_label = {s.label: s for s in segments}
    assert set(by_label) == {"S1", "S2", "S3", "S3a", "S3b"}
    s1 = by_label["S1"]
    assert (s1.start, s1.end, s1.litres, s1.days) == ("2023-06-01", "2024-10-31", 2945.0, 518)
    assert s1.usable is True and s1.gauge is False and s1.uncertainty_l == 0.0
    assert s1.top_level is True
    s2 = by_label["S2"]
    assert (s2.start, s2.end, s2.litres, s2.days) == ("2024-10-31", "2025-10-23", 2947.0, 357)
    assert s2.gauge is False and s2.top_level is True
    s3 = by_label["S3"]
    # S3 = 4500 - 1830 = 2670 L (gauge end point), exclusive day difference
    assert (s3.start, s3.end, s3.litres, s3.days) == ("2025-10-23", "2026-11-09", 2670.0, 382)
    assert s3.gauge is True and s3.uncertainty_l == kpi.TANK_GAUGE_UNCERTAINTY_L
    assert s3.top_level is True
    s3a = by_label["S3a"]
    assert (s3a.litres, s3a.days, s3a.top_level) == (2430.0, 302, False)
    s3b = by_label["S3b"]
    assert (s3b.litres, s3b.days, s3b.top_level) == (240.0, 80, False)
    # consistency: S3a + S3b = S3 total
    assert s3a.litres + s3b.litres == s3.litres


def test_build_segments_q127_canon_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_segments: 5 anchors (usable) + gauge-sequence HISTORIE
    (all usable=False), A1/A2 resolved in the tank metadata."""
    monkeypatch.setattr(
        kpi,
        "KPI_ANCHOR_SEGMENTS",
        (
            ("S1", "2030-01-05", "2030-01-20", 60.0, True, "syn S1"),
            ("S2", "2030-01-20", "2030-03-01", 100.0, True, "syn S2"),
            ("S3", "2030-03-01", "2030-03-15", 40.0, False, "syn S3"),
        ),
    )
    monkeypatch.setattr(kpi, "TANK_VALID_FROM", "2030-02-01")
    meas = [
        kpi.TankMeasurement("2030-01-02", 1500, 4500.0, True, 4000.0),
        kpi.TankMeasurement("2030-01-20", 1480, 4440.0, False, None),
        kpi.TankMeasurement("2030-02-01", 1500, 4500.0, True, 600.0),
        kpi.TankMeasurement("2030-03-01", 1650, 4650.0, False, None),
        kpi.TankMeasurement("2030-03-15", 1650, 4650.0, False, None),
    ]
    segments, tank = kpi.build_segments(meas, "2030-01-10", "2031-01-08")
    anchors = [s for s in segments if s.label]
    hist = [s for s in segments if not s.label]
    assert len(anchors) == 3 and len(hist) == 4
    # anchors: usable, labelled, S3 gauge-flagged
    assert all(s.usable for s in anchors)
    s3 = next(s for s in anchors if s.label == "S3")
    assert s3.gauge is True and s3.uncertainty_l == kpi.TANK_GAUGE_UNCERTAINTY_L
    assert s3.top_level is True
    # S1 starts before the window -> partial flag kept
    s1 = next(s for s in anchors if s.label == "S1")
    assert s1.partial is True and s1.in_window is True
    # history: ALL unusable (Q-127: no KPI from the gauge sequence)
    assert all(not s.usable for s in hist)
    pre = [s for s in hist if s.start < "2030-02-01"]
    assert pre  # at least the 2030-01-02 -> 2030-01-20 pair
    assert all("HISTORIE" in s.note and "TANK_VALID_FROM" in s.note for s in pre)
    # the in-canon-date pairs get the "außerhalb des Anker-Kanons" note
    out = [s for s in hist if s.start >= "2030-02-01"]
    assert out and all("Anker-Kanon" in s.note for s in out)
    # negative gauge pair gets the Fehlmess-Kandidat suffix
    neg = [s for s in hist if s.litres < 0]
    assert neg and all("Fehlmess-Kandidat" in s.note for s in neg)
    # tank metadata: A1/A2 RESOLVED, no old variant keys
    an = tank["anomalies"]
    assert set(an) == {"a1", "a2"}
    assert "gelöst" in an["a1"]["status"] and "Eintrags-Versatz" in an["a1"]["explanation"]
    assert "gelöst" in an["a2"]["status"] and "31.10.24" in an["a2"]["explanation"]
    assert "variant_a" not in an["a1"]
    assert tank["tank_valid_from"] == "2030-02-01"


def _baseline_daily() -> list[kpi.DayAgg]:
    """Boundary-exclusion test set on the synthetic anchor canon.

    * 01-05: S1 START day -> excluded from S1 (counted nowhere)
    * 01-06 .. 01-10: S1 interior (5 x 4 h, o6 2.0 each)
    * 03-01: S1 END (included in S1) AND S2 start (excluded from S2)
    * 03-02 .. 03-06: S2 interior (5 x 4 h, o6 1.0 each)
    Expected baseline: eff = 24 (S1) + 20 (S2) = 44 h, O6 = 10 + 5 = 15 h.
    An inclusive-start (double-counting) implementation would give 48 h.
    """
    base = dict(
        n_samples=100,
        raw_hours=4.0,
        eff_hours=4.0,
        cold30_hours=0.0,
        a5_max=75.0,
        a5_avg=60.0,
        ext_avg=5.0,
        wood_hours=0.0,
    )
    return (
        [
            kpi.DayAgg(day="2030-01-05", o6_hours=0.0, **base),  # S1 start (excl.)
        ]
        + [
            # S1 interior (start day 01-05 excluded): 01-06 .. 01-10
            kpi.DayAgg(day=f"2030-01-{d:02d}", o6_hours=2.0, **base)
            for d in range(6, 11)
        ]
        + [
            kpi.DayAgg(day="2030-03-01", o6_hours=0.0, **base),  # S1 end / S2 start
        ]
        + [
            # S2 interior (start day 03-01 excluded): 03-02 .. 03-06
            kpi.DayAgg(day=f"2030-03-{d:02d}", o6_hours=1.0, **base)
            for d in range(2, 7)
        ]
    )


def test_compute_old_burner_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline S1+S2 with the exclusive-start convention (no boundary-day
    double counting), O6 share included (synthetic anchor canon)."""
    monkeypatch.setattr(kpi, "KPI_ANCHOR_SEGMENTS", _synthetic_anchor_segments())
    segs = kpi.build_anchor_segments("2030-01-01", "2031-01-08")
    ob = kpi.compute_old_burner_baseline(segs, _baseline_daily())
    assert ob is not None
    # S1: 01-06..01-10 + 03-01 (start 01-05 excl.) = 6 x 4 h
    # S2: 03-02..03-06 (start 03-01 excl.) = 5 x 4 h -> total 44 h (NOT 48)
    assert ob.eff_hours == pytest.approx(44.0, abs=0.01)
    # synthetic S1+S2 = 400 + 350 L
    assert ob.litres == pytest.approx(750.0)
    assert ob.l_per_h == pytest.approx(750.0 / 44.0, abs=1e-3)
    # O6: 5 x 2.0 (S1) + 5 x 1.0 (S2) = 15 h -> 34.1 %
    assert ob.o6_hours == pytest.approx(15.0, abs=0.01)
    assert ob.o6_share_pct == pytest.approx(100.0 * 15.0 / 44.0, abs=0.1)


def test_compute_old_burner_baseline_missing_anchors() -> None:
    """No anchors -> None (e.g. synthetic fixture without S1/S2 labels)."""
    daily = _baseline_daily()
    segs = [
        kpi.TankSegment(
            label="S3", start="2030-06-01", end="2031-01-08", litres=10.0, days=2, usable=True
        )
    ]
    assert kpi.compute_old_burner_baseline(segs, daily) is None
    assert kpi.compute_old_burner_baseline([], daily) is None


def test_enrich_anchor_segments_exclusive_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchor segments sum hours over [start+1 .. end] (Q-127 convention)."""
    monkeypatch.setattr(kpi, "KPI_ANCHOR_SEGMENTS", _synthetic_anchor_segments())
    # one full-12h day on each boundary day, one day inside each span
    daily = [
        kpi.DayAgg(
            day=day,
            n_samples=100,
            raw_hours=12.0,
            eff_hours=12.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=1.0,
        )
        for day in (
            "2030-01-05",  # S1 start (excl. for S1)
            "2030-01-10",  # inside S1
            "2030-03-01",  # S1 end (incl. for S1) AND S2 start (excl. for S2)
            "2030-04-05",  # inside S2
            "2030-06-01",  # S2 end (incl. for S2) AND S3 start (excl. for S3)
            "2030-07-01",  # inside S3 + S3a
            "2030-09-01",  # S3a end (incl.) AND S3b start (excl. for S3b)
        )
    ]
    segs = kpi.build_anchor_segments("2030-01-01", "2031-01-08")
    kpi.enrich_segments(segs, daily, date(2030, 1, 1), date(2031, 1, 8))
    by = {s.label: s for s in segs}
    # S1: 01-10 + 03-01 (start 01-05 excluded)
    assert by["S1"].hours_eff == pytest.approx(24.0, abs=0.01)
    assert by["S1"].o6_hours == pytest.approx(2.0, abs=0.01)
    # S2: 04-05 + 06-01 (start 03-01 excluded - not double-counted with S1)
    assert by["S2"].hours_eff == pytest.approx(24.0, abs=0.01)
    # S3: 07-01 + 09-01 (start 06-01 excluded)
    assert by["S3"].hours_eff == pytest.approx(24.0, abs=0.01)
    # S3a (06-01 -> 09-01, excl. start): 07-01 + 09-01
    assert by["S3a"].hours_eff == pytest.approx(24.0, abs=0.01)
    # S3b (09-01 -> 01-08, excl. start): no synthetic day falls strictly
    # inside -> 0 h (boundary case of the exclusive-start convention)
    assert by["S3b"].hours_eff == pytest.approx(0.0, abs=0.01)


def test_enrich_gauge_segments_inclusive_and_monthly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gauge-sequence segments keep the INCLUSIVE span; sub-segments must
    never emit monthly estimates (they overlap S3)."""
    monkeypatch.setattr(kpi, "KPI_ANCHOR_SEGMENTS", _synthetic_anchor_segments())
    daily = [
        kpi.DayAgg(
            day=f"2030-02-{d:02d}",
            n_samples=100,
            raw_hours=1.0,
            eff_hours=1.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
        )
        for d in range(1, 29)
    ]
    # synthetic anchor canon (fixture constants): S1 fully covers 2030-02
    segments = kpi.build_anchor_segments("2030-01-01", "2031-01-08")
    kpi.enrich_segments(segments, daily, date(2030, 1, 1), date(2031, 1, 8))
    by = {s.label: s for s in segments}
    # S1 (01-05 -> 03-01) fully covers February; rate = 400/55 L/d
    s1_months = [m["month"] for m in by["S1"].monthly_estimated]
    assert "2030-02" in s1_months  # Jan is only partially inside S1
    rate = 400.0 / 55
    est = next(m for m in by["S1"].monthly_estimated if m["month"] == "2030-02")
    assert est["litres"] == pytest.approx(rate * 28, abs=0.1)
    assert est["estimated"] is True
    # sub-segments: NO monthly estimates (they overlap S3)
    assert by["S3a"].monthly_estimated == []
    assert by["S3b"].monthly_estimated == []
    # S3 still carries its own monthly estimates for full months inside it
    assert any(m["month"] == "2030-07" for m in by["S3"].monthly_estimated)


def test_enrich_partial_anchor_pro_rata(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anchor segment starting before the window: pro-rata primary L/h +
    naive bias-flagged L/h over the in-window hours."""
    monkeypatch.setattr(
        kpi,
        "KPI_ANCHOR_SEGMENTS",
        (
            ("S1", "2030-01-01", "2030-01-21", 60.0, True, "syn S1"),
            ("S2", "2030-03-01", "2030-04-01", 50.0, True, "syn S2"),
            ("S3", "2030-04-01", "2030-04-20", 20.0, False, "syn S3"),
        ),
    )
    daily = [
        kpi.DayAgg(
            day=f"2030-01-{d:02d}",
            n_samples=100,
            raw_hours=5.0,
            eff_hours=5.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
        )
        for d in range(1, 22)  # all 21 days present
    ]
    segments = kpi.build_anchor_segments("2030-01-10", "2031-01-08")
    kpi.enrich_segments(segments, daily, date(2030, 1, 10), date(2031, 1, 8))
    s1 = segments[0]
    assert s1.partial is True
    # exclusive-start convention: hours from 01-10 .. 01-21 = 12 days x 5 h
    assert s1.hours_eff == pytest.approx(60.0, abs=0.01)
    # pro-rata primary: 60 L x (12 in-window d / 20 total d) / 60 h
    # total days = exclusive diff = 20 (01-01 -> 01-21)
    assert s1.l_per_h_eff == pytest.approx(60.0 * (12 / 20) / 60.0, abs=1e-3)
    # naive: 60 L / 60 h (bias-flagged overestimate)
    assert s1.l_per_h_naive == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------
# aggregations
# ---------------------------------------------------------------------------
def test_group_by_month_year_season() -> None:
    daily = [
        kpi.DayAgg(
            day="2030-01-15",
            n_samples=100,
            raw_hours=5.0,
            eff_hours=5.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=10.0,
            o6_hours=3.0,
        ),
        kpi.DayAgg(
            day="2030-01-16",
            n_samples=100,
            raw_hours=5.0,
            eff_hours=0.0,
            cold30_hours=5.0,
            a5_max=20.0,
            a5_avg=20.0,
            ext_avg=3.0,
            wood_hours=0.0,
            o6_hours=1.0,
            is_failure=True,
        ),
    ]
    monthly = kpi.group_by_month(daily)
    rec = monthly["2030-01"]
    assert rec["raw_h"] == pytest.approx(10.0)
    assert rec["eff_h"] == pytest.approx(5.0)  # failure day excluded
    assert rec["ausfall_tage"] == 1
    assert rec["ext_avg"] == pytest.approx(4.0)
    assert rec["hdd15"] == pytest.approx(22.0)  # (15 - 5) + (15 - 3)
    assert rec["holzstunden"] == pytest.approx(10.0)
    assert rec["o6stunden"] == pytest.approx(4.0)

    yearly = kpi.group_by_year(daily)
    assert yearly["2030"]["eff_h"] == pytest.approx(5.0)

    seasons = kpi.group_by_season(daily)
    assert (2029, 2030) in seasons
    s = seasons[(2029, 2030)]
    assert s["eff_h"] == pytest.approx(5.0)
    assert s["o6_summer_h"] == 0.0


def test_group_by_season_summer_o6() -> None:
    daily = [
        kpi.DayAgg(
            day="2030-07-15",
            n_samples=100,
            raw_hours=0.0,
            eff_hours=0.0,
            cold30_hours=0.0,
            a5_max=50.0,
            a5_avg=50.0,
            ext_avg=20.0,
            wood_hours=0.0,
            o6_hours=5.0,
        ),
        kpi.DayAgg(
            day="2030-11-15",
            n_samples=100,
            raw_hours=5.0,
            eff_hours=5.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=2.0,
            o6_hours=1.0,
        ),
    ]
    seasons = kpi.group_by_season(daily)
    assert (2030, 2031) in seasons
    s = seasons[(2030, 2031)]
    assert s["eff_h"] == pytest.approx(5.0)
    assert s["holz_h"] == pytest.approx(2.0)
    # summer O6 attributed to the FOLLOWING season
    assert s["o6_summer_h"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# trend fit + season L/h rows
# ---------------------------------------------------------------------------
def test_trend_fit() -> None:
    points = [(0.0, 5.0), (365.0, 5.5), (730.0, 6.0)]
    fit = kpi.trend_fit(points)
    assert fit["n"] == 3
    assert fit["slope_l_per_h_per_year"] == pytest.approx(0.5 * 365.25 / 365.0, abs=0.01)
    assert fit["r2"] == pytest.approx(1.0)
    # degenerate inputs
    single = kpi.trend_fit([(0.0, 1.0)])
    assert single["n"] == 1 and single["slope_l_per_h_per_year"] is None
    empty = kpi.trend_fit([])
    assert empty["n"] == 0 and empty["r2"] is None
    # flat data -> r2 None (no variance)
    flat = kpi.trend_fit([(0.0, 2.0), (10.0, 2.0)])
    assert flat["r2"] is None


def test_build_trend_primary_excludes_partial() -> None:
    """Primary = full seasons only; including_partial adds the running one."""
    rows = [
        # 3 full seasons (213 days covered) with L/h rising gently
        {"key": [2023, 2024], "l_per_h": 0.835, "days_covered": 213, "is_partial": False},
        {"key": [2024, 2025], "l_per_h": 0.979, "days_covered": 213, "is_partial": False},
        {"key": [2025, 2026], "l_per_h": 0.984, "days_covered": 213, "is_partial": False},
        # running partial season: high L/h must NOT leak into the primary fit
        {"key": [2026, 2027], "l_per_h": 3.594, "days_covered": 40, "is_partial": True},
    ]
    trend = kpi.build_trend(rows)
    assert set(trend) == {"primary", "including_partial", "note"}
    assert trend["primary"]["n"] == 3
    assert trend["including_partial"]["n"] == 4
    # the partial season (3.59 L/h) dominates the 4-point fit; the 3-point
    # full-season fit is flat in comparison
    assert trend["including_partial"]["slope_l_per_h_per_year"] is not None
    assert (
        trend["primary"]["slope_l_per_h_per_year"]
        < trend["including_partial"]["slope_l_per_h_per_year"]
    )
    assert "Voll-Saisons" in trend["note"]
    # all seasons partial -> primary has NO points (n = 0), secondary can fit
    all_partial = [dict(r, is_partial=True, days_covered=40) for r in rows[:2]]
    trend2 = kpi.build_trend(all_partial)
    assert trend2["primary"]["n"] == 0 and trend2["primary"]["slope_l_per_h_per_year"] is None
    assert trend2["including_partial"]["n"] == 2
    # fewer than 2 full seasons but enough total rows
    one_full = [
        {"key": [2025, 2026], "l_per_h": 0.984, "days_covered": 213, "is_partial": False},
        {"key": [2026, 2027], "l_per_h": 3.594, "days_covered": 40, "is_partial": True},
    ]
    trend3 = kpi.build_trend(one_full)
    assert trend3["primary"]["n"] == 1 and trend3["primary"]["slope_l_per_h_per_year"] is None
    assert trend3["including_partial"]["n"] == 2


def test_season_trend_rows_top_level_only() -> None:
    """Sub-segments (S3a/S3b) must NOT be attributed additively."""
    segments = [
        # 4 days (01-05 .. 01-08 inclusive) -> rate x 4 = 500 L attributed
        kpi.TankSegment(
            start="2030-01-05",
            end="2030-01-08",
            litres=500.0,
            days=4,
            usable=True,
        ),
        # overlapping sub-segment over the same span: must be ignored
        kpi.TankSegment(
            start="2030-01-05",
            end="2030-01-08",
            litres=500.0,
            days=4,
            usable=True,
            top_level=False,
        ),
    ]
    season_hours = {
        (2029, 2030): {
            "raw_h": 46.4,
            "eff_h": 46.4,
            "ext_avg": 4.0,
            "hdd15": 400.0,
            "holz_h": 5.0,
            "o6_summer_h": 10.0,
        },
        (2030, 2031): {
            "raw_h": 0.0,
            "eff_h": 0.0,
            "ext_avg": 20.0,
            "hdd15": 0.0,
            "holz_h": 0.0,
            "o6_summer_h": 0.0,
        },
    }
    rows = kpi.season_trend_rows(segments, season_hours)
    assert len(rows) == 1
    row = rows[0]
    assert row["season"] == "2029/30"  # Jan-2030 belongs to the 2029/30 season
    assert row["l_per_h"] == pytest.approx(500.0 / 46.4, abs=1e-3)  # NOT 1000/46.4
    assert row["days_covered"] == 4
    assert row["is_partial"] is True  # 4 days < 200 -> partial season
    # wood share <= 100 %: reported as percentage, no note
    assert row["holz_share_pct"] == pytest.approx(100.0 * 5.0 / 46.4, abs=0.1)
    assert row["holz_note"] == ""
    # unusable segments are ignored
    segments[0].usable = False
    assert kpi.season_trend_rows(segments, season_hours) == []


def test_season_trend_rows_wood_dominant() -> None:
    """holz_h > eff_h: share is None + note (wood was the primary source),
    NOT a capped or >100 % value.  Equality (holz_h == eff_h) is 100 %."""
    segments = [
        kpi.TankSegment(start="2030-01-05", end="2030-01-14", litres=500.0, days=10, usable=True),
    ]
    season_hours = {
        (2029, 2030): {
            "raw_h": 46.4,
            "eff_h": 46.4,
            "ext_avg": 4.0,
            "hdd15": 400.0,
            "holz_h": 60.0,  # EXCEEDS eff_h -> wood dominated the period
            "o6_summer_h": 0.0,
        },
    }
    rows = kpi.season_trend_rows(segments, season_hours)
    assert len(rows) == 1
    row = rows[0]
    assert row["holz_share_pct"] is None
    assert row["holz_note"] == "Holz-h > Öl-h (Holz primäre Wärmequelle im Zeitraum)"
    # boundary: holz_h == eff_h -> exactly 100 % (still meaningful)
    season_hours[(2029, 2030)]["holz_h"] = 46.4
    row2 = kpi.season_trend_rows(segments, season_hours)[0]
    assert row2["holz_share_pct"] == 100.0
    assert row2["holz_note"] == ""


def test_collect_monthly_estimates() -> None:
    segs = [
        kpi.TankSegment(
            start="2030-01-01",
            end="2030-03-01",
            litres=100.0,
            days=60,
            usable=True,
            monthly_estimated=[
                {"month": "2030-02", "litres": 30.0, "rate_l_per_day": 1.1, "estimated": True}
            ],
        ),
        kpi.TankSegment(
            start="2030-02-01",
            end="2030-04-01",
            litres=100.0,
            days=60,
            usable=True,
            top_level=False,
            monthly_estimated=[
                {"month": "2030-03", "litres": 40.0, "rate_l_per_day": 1.3, "estimated": True}
            ],
        ),
    ]
    merged = kpi.collect_monthly_estimates(segs)
    # sub-segments never produce monthly estimates (overlapping S3)
    assert [m["month"] for m in merged] == ["2030-02"]
    assert merged[0]["litres"] == 30.0
    assert len(merged[0]["sources"]) == 1


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------
def test_write_monthly_csv(tmp_path: Path) -> None:
    monthly = {
        "2030-01": {
            "month": "2030-01",
            "raw_h": 10.0,
            "eff_h": 5.0,
            "ausfall_tage": 1,
            "ext_avg": 4.0,
            "hdd15": 24.0,
            "holzstunden": 10.0,
            "o6stunden": 4.0,
        }
    }
    out = tmp_path / "burn_hours_monthly.csv"
    kpi.write_monthly_csv(out, monthly)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "month;roh_h;effektive_h;ausfall_tage;ext_avg;hdd15;holzstunden;o6stunden"
    assert lines[1].startswith("2030-01;")


def test_fmt_helpers() -> None:
    assert kpi.fmt_h(1234.56) == "1.234,6"
    assert kpi.fmt_h(None) == "n/a"
    assert kpi.fmt_lph(1.234) == "1,23"
    assert kpi.fmt_num(7.0) == "7,0"


def test_md_table() -> None:
    t = kpi._md_table(["a", "b"], [["1", "2"], ["3", "4"]])
    assert t == "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"


# ---------------------------------------------------------------------------
# end-to-end on synthetic data
# ---------------------------------------------------------------------------
def test_end_to_end_synthetic(synthetic: dict[str, object]) -> None:
    payload = run_analysis(synthetic)
    totals = payload["totals"]
    # raw: 2030 = 4 normal x 12 h + 4 failure x 10 h + intermittent 10 h
    #       + ramp 8 h + 2 normal Feb x 12 h = 106 h; 2031 = 4 x 12 h
    assert totals["raw_hours"] == pytest.approx(154.0, abs=0.01)
    assert totals["failure_days_candidate"] == 5
    assert totals["failure_days_strict"] == 4
    assert totals["failure_windows"] == 1
    win = payload["failures"]["windows"][0]
    assert win["start"] == "2030-01-14" and win["end"] == "2030-01-18"
    assert win["candidate_days"] == 5
    assert win["strict_days"] == 4  # all four failure days: a5 max 20 < 40
    assert win["intermittent_days"] == 1  # 01-18 (a5 max 65)
    # sensitivity block has all three thresholds
    assert set(payload["sensitivity_t_ign"]) == {"T_28_deg", "T_30_deg", "T_35_deg"}
    # Q-127 segments: 5 KPI anchors (usable, labelled) + 4 HISTORIE gauge
    # segments (all usable=False - NO KPI from the gauge sequence)
    segs = payload["segments"]
    anchors = [s for s in segs if s["label"]]
    hist = [s for s in segs if not s["label"]]
    assert [s["label"] for s in anchors] == ["S1", "S2", "S3", "S3a", "S3b"]
    assert all(s["usable"] for s in anchors)
    assert {s["label"]: s["top_level"] for s in anchors} == {
        "S1": True,
        "S2": True,
        "S3": True,
        "S3a": False,
        "S3b": False,
    }
    s3 = next(s for s in anchors if s["label"] == "S3")
    assert s3["gauge"] is True and s3["uncertainty_l"] == 30.0
    assert len(hist) == 4 and all(not s["usable"] for s in hist)
    assert all("HISTORIE" in s["note"] for s in hist)
    # old-burner baseline: S1+S2 = 400+350 L; exclusive-start hours over the
    # 2030-01-10 .. 2030-06-01 span: 2 normal (11,4) + intermittent (0,03)
    # + ramp (4,0) + 2 normal Feb (11,4) = 49,63 h
    ob = payload["old_burner_baseline"]
    assert ob["litres"] == pytest.approx(750.0)
    assert ob["eff_hours"] == pytest.approx(49.63, abs=0.01)
    assert ob["l_per_h"] == pytest.approx(750.0 / 49.6333, abs=0.01)
    # O6 share: only synthetic O6 hours are 0 -> 0 % (o6 channel all zeros)
    assert ob["o6_hours"] == pytest.approx(0.0)
    # anomalies resolved (A1/A2 geloesst), tank valid from patched
    an = payload["oil"]["anomalies"]
    assert set(an) == {"a1", "a2"} and all("gelöst" in v["status"] for v in an.values())
    assert payload["oil"]["tank_valid_from"] == "2030-01-05"
    # monthly oil estimate: S1 (01-05 -> 03-01) fully contains 2030-02; S2
    # fully contains no whole month; S3 (06-01 -> 01-08) fully contains
    # 2030-06 .. 2030-12. Sub-segments contribute NOTHING.
    est = payload["oil"]["monthly_estimated"]
    est_months = [e["month"] for e in est]
    assert "2030-02" in est_months
    assert "2030-07" in est_months  # fully inside S3
    for e in est:
        assert len(e["sources"]) == 1  # never merged from S3 + sub-segment
    # burn hours yearly
    assert payload["burn_hours"]["yearly"]["2030"]["raw_h"] == pytest.approx(106.0, abs=0.01)
    assert payload["burn_hours"]["yearly"]["2031"]["raw_h"] == pytest.approx(48.0, abs=0.01)
    # season L/h: exactly 2 rows (2029/30 covered by S1+S2, 2030/31 by S3);
    # sub-segments must NOT double-count 2030/31
    rows = payload["season_lph"]
    assert [r["season"] for r in rows] == ["2029/30", "2030/31"]
    assert rows[0]["l_per_h"] == pytest.approx(639.34 / 49.6333, abs=0.02)
    assert rows[1]["l_per_h"] == pytest.approx(117.65 / 45.6, abs=0.02)
    # synthetic window is short -> both seasons are partial (< 200 days)
    assert [r["is_partial"] for r in rows] == [True, True]
    # synthetic a9 = 50 deg C (< 65 WOOD_ACTIVE_DEG) -> no wood hours,
    # share = 0 % (meaningful, no note)
    for r in rows:
        assert r["holz_share_pct"] == 0.0 and r["holz_note"] == ""
    # trend: new primary / including_partial structure - all seasons partial
    # -> primary has no points, the secondary fit still has n = 2
    trend = payload["trend"]
    assert set(trend) == {"primary", "including_partial", "note"}
    assert trend["primary"]["n"] == 0
    assert trend["primary"]["slope_l_per_h_per_year"] is None
    assert trend["including_partial"]["n"] == 2
    assert trend["including_partial"]["slope_l_per_h_per_year"] is not None
    # report is German and contains the key sections + Q-127 canon
    out = Path(str(synthetic["out"]))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Brenndauer" in report
    assert "Ausfall-Fenster" in report
    assert "2030-01-14" in report
    assert "KPI-Anker-Segmente" in report
    assert "Alter Brenner" in report
    assert "HISTORIE" in report
    assert "Voll-Saisons" in report  # trend block uses the primary wording


def test_run_writes_artifacts_and_returns_payload(synthetic: dict[str, object]) -> None:
    payload = run_analysis(synthetic)
    assert payload["meta"]["t_ign_deg"] == 30.0
    assert payload["window_stats"]["observed_days"] == 15
    out = Path(str(synthetic["out"]))
    # CSV: header + one row per observed month (2030-01, 2030-02, 2031-01)
    lines = (out / "burn_hours_monthly.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + 3


# ---------------------------------------------------------------------------
# real-data smoke tests (only with -m slow)
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_real_jan2026_failure_window() -> None:
    """Jan-2026-Ausfall-Fenster 14.01.-29.01. wird erkannt (Real-Data)."""
    _require_real_data()
    con = kpi.make_connection()
    try:
        daily = kpi.load_daily(
            con,
            kpi.DEFAULT_PARQUET_GLOBS,
            "2026-01-13",
            "2026-01-30",
            30.0,
        )
    finally:
        con.close()
    failures = kpi.detect_failure_days(daily)
    windows = kpi.group_failure_windows(failures)
    assert len(windows) == 1
    win = windows[0]
    assert win.start == "2026-01-14"
    assert win.end == "2026-01-29"
    assert win.candidate_days == 15
    assert win.strict_days == 5
    # Jan-2026 is NOT in the Oct-2025 override set
    assert win.overridden_days == 0
    assert win.note == ""


@pytest.mark.slow
def test_real_oct2025_override_window() -> None:
    """Okt-2025-Fenster: maintainer-Override -> 0 strikte Tage, Note gesetzt.

    Die 5 Tage 13.-17.10.2025 werden von strikt auf intermittierend
    umklassifiziert (overridden), das Fenster bleibt gelistet, aber nichts
    wird exkludiert.
    """
    _require_real_data()
    con = kpi.make_connection()
    try:
        daily = kpi.load_daily(
            con,
            kpi.DEFAULT_PARQUET_GLOBS,
            "2025-10-11",
            "2025-10-22",
            30.0,
        )
    finally:
        con.close()
    failures = kpi.detect_failure_days(daily, apply_oct2025_override=True)
    ovr = {f.day: f for f in failures}
    for d in ("2025-10-13", "2025-10-14", "2025-10-15", "2025-10-16", "2025-10-17"):
        assert ovr[d].kind == "intermittent"
        assert ovr[d].overridden is True
    windows = kpi.group_failure_windows(failures)
    assert len(windows) == 1
    win = windows[0]
    assert (win.start, win.end) == ("2025-10-12", "2025-10-21")
    assert win.strict_days == 0
    assert win.overridden_days == 5
    assert win.intermittent_days == 9
    assert win.excluded_cold_hours == 0.0
    assert "maintainer-Override" in win.note


@pytest.mark.slow
def test_real_q127_anchor_and_era_numbers() -> None:
    """Q-127-Anker, Baseline, Era und Saisons (Real-Data).

    Alle Referenzwerte wurden per CLI-Run aus ``tmp/oel_kpi/oel_kpi.json``
    abgelesen (Orchestrator-Vergabe 2026-11-10), NICHT geraten:
    S1 1.687 / S2 1.546 / S3 1.638 [1.45-1.82] / Baseline 1.614 /
    Era 786.24 h / s3_rate 2.498 / sub_split 2.362 / Mini 2.247 [0.00-5.06] /
    Trend primary n=3 +0.074 (R² 0.78) vs. incl. Teil-Saison n=4 +0.828
    (R² 0.64) / 2026/27 Holz-Anteil None (Holz dominiert).

    Live-Daten-Drift 2026-11-12 (cmi_log_pq/2026-11.parquet neu geloggt,
    11.10 -> 11.12): zwei Werte des laufenden Fensters wurden per CLI-Run
    NEU abgelesen - incl. Teil-Saison slope 0.828 -> 0.826 (R² 0.643) und
    totals.effective_hours 11103.42 -> 11103.53. Der Rest (Segmente,
    Baseline, Era, Saisons) ist auf festen Spannen stabil.
    """
    _require_real_data()
    payload = kpi.run(
        REAL_START,
        REAL_END,
        30.0,
        Path("tmp/oel_kpi_smoke"),
        Path("tmp/tank_zollstock_2015_2026.csv"),
    )
    by_label = {s["label"]: s for s in payload["segments"] if s["label"]}
    assert set(by_label) == {"S1", "S2", "S3", "S3a", "S3b"}
    # KPI anchors: usable, S1/S2 exact, S3 gauge
    assert all(s["usable"] for s in by_label.values())
    assert by_label["S1"]["gauge"] is False and by_label["S2"]["gauge"] is False
    assert by_label["S3"]["gauge"] is True
    assert by_label["S3a"]["top_level"] is False and by_label["S3b"]["top_level"] is False
    # L/h per anchor (exclusive-start convention: S2 counts 01.11.24 .. 23.10.25)
    assert by_label["S1"]["hours_eff"] == pytest.approx(1745.83, abs=0.1)
    assert by_label["S1"]["l_per_h_eff"] == pytest.approx(1.687, abs=0.005)
    assert by_label["S2"]["hours_eff"] == pytest.approx(1905.68, abs=0.1)
    assert by_label["S2"]["l_per_h_eff"] == pytest.approx(1.546, abs=0.005)
    assert by_label["S3"]["hours_eff"] == pytest.approx(1629.64, abs=0.1)
    assert by_label["S3"]["l_per_h_eff"] == pytest.approx(1.638, abs=0.005)
    # HISTORIE: gauge-sequence segments are all unusable
    hist = [s for s in payload["segments"] if not s["label"]]
    assert hist and all(not s["usable"] for s in hist)
    assert all("HISTORIE" in s["note"] for s in hist)
    # anomalies resolved (Q-127)
    an = payload["oil"]["anomalies"]
    assert "gelöst" in an["a1"]["status"] and "gelöst" in an["a2"]["status"]
    assert payload["oil"]["tank_valid_from"] == "2023-06-01"
    # old-burner baseline: S1+S2 = 5892 L / 3651.51 h = 1.614 L/h, O6 78.1 %
    ob = payload["old_burner_baseline"]
    assert ob["litres"] == pytest.approx(5892.0, abs=0.5)
    assert ob["eff_hours"] == pytest.approx(3651.51, abs=0.5)
    assert ob["l_per_h"] == pytest.approx(1.614, abs=0.002)
    assert ob["o6_hours"] == pytest.approx(2850.19, abs=0.5)
    assert ob["o6_share_pct"] == pytest.approx(78.1, abs=0.1)
    # era capped at S3 end (09.11.26), NOT the window end (10.11.26)
    era = payload["burner_era"]
    assert era is not None
    assert era["start"] == "2026-02-01" and era["end"] == "2026-11-09"
    assert era["eff_hours"] == pytest.approx(786.24, abs=0.1)
    assert era["o6_hours"] == pytest.approx(732.32, abs=0.1)
    assert era["strict_days"] == 0
    s3r = era["s3_rate"]
    assert s3r["oil_days"] == 281
    assert s3r["litres"] == pytest.approx(1964.06, abs=0.5)
    assert s3r["l_per_h"] == pytest.approx(2.498, abs=0.005)
    sub = era["sub_split"]
    assert sub["split_days"] == 201
    assert sub["litres"] == pytest.approx(1857.32, abs=0.5)
    assert sub["l_per_h"] == pytest.approx(2.362, abs=0.005)
    mini = era["mini_segment"]
    assert mini["l_per_h"] == pytest.approx(2.247, abs=0.005)
    assert mini["uncertainty"] == [0.0, 5.06]
    # seasons: exactly 4 (Q-127: only segments from 06/2023 usable); the
    # sub-segments must NOT double-count 2025/26 (spec ~1511 L / ~0.98)
    rows = payload["season_lph"]
    assert [r["season"] for r in rows] == ["2023/24", "2024/25", "2025/26", "2026/27"]
    assert rows[0]["l_per_h"] == pytest.approx(0.835, abs=0.005)
    assert rows[1]["l_per_h"] == pytest.approx(0.979, abs=0.005)
    assert rows[2]["l_per_h"] == pytest.approx(0.984, abs=0.005)
    assert rows[2]["litres_season"] == pytest.approx(1517.87, abs=5.0)
    # full vs partial season flags
    assert [r["is_partial"] for r in rows] == [False, False, False, True]
    assert rows[3]["days_covered"] == 40  # partial season 2026/27 (running)
    # 2026/27: record wood month (Okt-2026), holz_h 321 h > oil eff 78 h
    # -> share None + note, NOT a value > 100 %
    assert rows[3]["holz_share_pct"] is None
    assert rows[3]["holz_note"] == "Holz-h > Öl-h (Holz primäre Wärmequelle im Zeitraum)"
    # the full seasons have a meaningful share and no note
    for r in rows[:3]:
        assert r["holz_share_pct"] is not None and 0.0 <= r["holz_share_pct"] <= 100.0
        assert r["holz_note"] == ""
    # trend: new structure (read from the regenerated CLI JSON, not guessed):
    # primary = 3 full seasons -> nearly flat; including_partial = 4 -> the
    # partial season dominates the slope (artifact, not proof of drift)
    trend = payload["trend"]
    assert set(trend) == {"primary", "including_partial", "note"}
    assert trend["primary"]["n"] == 3
    assert trend["primary"]["slope_l_per_h_per_year"] == pytest.approx(0.074, abs=0.002)
    assert trend["primary"]["r2"] == pytest.approx(0.776, abs=0.005)
    assert trend["including_partial"]["n"] == 4
    # Drift 2026-11-12: laufende Teil-Saison 2026/27 waechst mit neuen
    # Daten-Tagen -> 0.826 (Stand: cmi_log_pq bis 11.11, 11.12 unvollstaendig)
    assert trend["including_partial"]["slope_l_per_h_per_year"] == pytest.approx(0.826, abs=0.002)
    assert trend["including_partial"]["r2"] == pytest.approx(0.643, abs=0.005)
    # monthly oil estimates only from S1/S2/S3 (no double-counted sources)
    est = payload["oil"]["monthly_estimated"]
    assert est and all(len(e["sources"]) == 1 for e in est)
    assert all(e["month"] >= "2023-06" for e in est)
    # totals: effective burn time UNCHANGED by the tank model; Jan-2026 is
    # the only excluded total failure (override active by default)
    totals = payload["totals"]
    assert totals["failure_days_strict"] == 5
    assert totals["failure_days_raw_hours"] == pytest.approx(117.64, abs=0.1)
    # Drift 2026-11-12: +~0.11 h effektive Brennstunden (11.10 ist jetzt
    # nahezu komplett, 11.11 vollstaendig; 11.12 liegt außerhalb des
    # REAL_END-Fensters)
    assert totals["effective_hours"] == pytest.approx(11103.53, abs=0.1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_main_missing_tank_csv(
    synthetic: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = kpi.main(
        [
            "--out",
            str(Path(str(synthetic["out"]))),
            "--tank-csv",
            "does-not-exist.csv",
        ]
    )
    assert rc == 2


def test_parse_args_defaults() -> None:
    args = kpi.parse_args([])
    assert args.out == "tmp/oel_kpi"
    assert args.start == "2020-09-01"
    assert args.end == "2026-11-10"
    assert args.t_ign == 30.0
    assert args.no_oct2025_override is False  # override ACTIVE by default
    assert args.era_start == "2026-02-01"


def test_parse_args_override_and_era_flags() -> None:
    args = kpi.parse_args(["--no-oct2025-override", "--era-start", "2026-03-01"])
    assert args.no_oct2025_override is True
    assert args.era_start == "2026-03-01"


# ---------------------------------------------------------------------------
# new-burner era (Q-127 S3 scenarios)
# ---------------------------------------------------------------------------
def _era_daily() -> list[kpi.DayAgg]:
    """5 days Feb 2026 + 3 days Sep 2026 (inside the S3b mini-segment
    span, which also serves as the S3 end in this unit-level test)."""
    daily = [
        kpi.DayAgg(
            day=f"2026-02-0{d}",
            n_samples=100,
            raw_hours=3.0,
            eff_hours=2.0,
            cold30_hours=0.0,
            a5_max=70.0,
            a5_avg=65.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=1.0,
        )
        for d in range(1, 6)
    ]
    daily += [
        kpi.DayAgg(
            day=f"2026-09-0{d}",
            n_samples=100,
            raw_hours=4.0,
            eff_hours=5.0,
            cold30_hours=0.0,
            a5_max=70.0,
            a5_avg=65.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.5,
        )
        for d in range(1, 4)
    ]
    return daily


def _era_segments() -> list[kpi.TankSegment]:
    """Labeled S3 anchor segments of the real KPI canon (S3 ends
    2026-11-09 -> the era caps there; the synthetic era days all fall
    inside 2026-02-01 .. 2026-11-09)."""
    return [
        kpi.TankSegment(
            label="S3", start="2025-10-23", end="2026-11-09", litres=2670.0, days=382, usable=True
        ),
        kpi.TankSegment(
            label="S3a",
            start="2025-10-23",
            end="2026-08-21",
            litres=2430.0,
            days=302,
            usable=True,
            top_level=False,
        ),
        kpi.TankSegment(
            label="S3b",
            start="2026-08-21",
            end="2026-11-09",
            litres=240.0,
            days=80,
            usable=True,
            top_level=False,
        ),
    ]


def test_compute_burner_era_s3_scenarios() -> None:
    """Q-127 era: end capped at S3 end, s3_rate + sub_split + mini S3b."""
    era = kpi.compute_burner_era(
        _era_daily(), _era_segments(), "2026-02-01", "2026-11-10", failures=[]
    )
    assert era is not None
    # era end is min(window end, S3 end) -> 2026-11-09 (NOT the window end)
    assert (era.start, era.end) == ("2026-02-01", "2026-11-09")
    assert era.eff_hours == pytest.approx(25.0)  # 5x2.0 + 3x5.0
    assert era.raw_hours == pytest.approx(27.0)  # 5x3.0 + 3x4.0
    assert era.o6_hours == pytest.approx(6.5)  # 5x1.0 + 3x0.5
    assert era.ext_avg == pytest.approx(5.0)
    assert era.strict_days == 0
    assert [m["month"] for m in era.months] == ["2026-02", "2026-09"]
    assert era.months[0]["eff_hours"] == pytest.approx(10.0)
    assert era.months[1]["eff_hours"] == pytest.approx(15.0)
    # s3_rate: S3 rate (2670/382 L/d) over the exclusive era span
    # (01.02.26 -> 09.11.26 = 281 oil days)
    rate = 2670.0 / 382
    s3r = era.s3_rate
    assert s3r["oil_days"] == (date(2026, 11, 9) - date(2026, 2, 1)).days == 281
    assert s3r["rate_l_per_day"] == pytest.approx(rate, abs=1e-4)
    assert s3r["litres"] == pytest.approx(rate * 281, abs=1e-2)
    assert s3r["l_per_h"] == pytest.approx(rate * 281 / 25.0, abs=1e-3)
    # module rounds the band to 2 decimals
    assert s3r["uncertainty"] == [
        round(max(0.0, rate * 281 - kpi.TANK_GAUGE_UNCERTAINTY_L) / 25.0, 2),
        round((rate * 281 + kpi.TANK_GAUGE_UNCERTAINTY_L) / 25.0, 2),
    ]
    # sub_split: S3a rate over 01.02. -> 21.08. (201 d) + S3b litres
    rate_a = 2430.0 / 302
    sub = era.sub_split
    assert sub["split_days"] == (date(2026, 8, 21) - date(2026, 2, 1)).days == 201
    assert sub["s3a_rate_l_per_day"] == pytest.approx(rate_a, abs=1e-4)
    assert sub["s3b_litres"] == 240.0
    assert sub["litres"] == pytest.approx(rate_a * 201 + 240.0, abs=1e-2)
    assert sub["l_per_h"] == pytest.approx((rate_a * 201 + 240.0) / 25.0, abs=1e-3)
    # mini segment: L/h over its own in-segment hours (3 x 5.0 = 15 h)
    mini = era.mini_segment
    assert mini["start"] == "2026-08-21" and mini["end"] == "2026-11-09"
    assert mini["litres"] == 240.0
    assert mini["eff_hours"] == pytest.approx(15.0)
    assert mini["l_per_h"] == pytest.approx(16.0)
    # uncertainty band (conservative +/-TANK_GAUGE_UNCERTAINTY_L on the
    # segment; lower bound floored at 0; module rounds to 2 decimals)
    assert mini["uncertainty"] == [
        round(max(0.0, 240.0 - kpi.TANK_GAUGE_UNCERTAINTY_L) / 15.0, 2),
        round((240.0 + kpi.TANK_GAUGE_UNCERTAINTY_L) / 15.0, 2),
    ]
    # strict days in the era are counted from `failures`
    era2 = kpi.compute_burner_era(
        _era_daily(),
        _era_segments(),
        "2026-02-01",
        "2026-11-10",
        failures=[kpi.FailureDay("2026-02-01", "strict", 24.0, 24.0, 20.0, 20.0, 5.0)],
    )
    assert era2.strict_days == 1


def test_compute_burner_era_none_outside_window() -> None:
    daily = [
        kpi.DayAgg(
            day="2025-01-01",
            n_samples=100,
            raw_hours=1.0,
            eff_hours=1.0,
            cold30_hours=0.0,
            a5_max=70.0,
            a5_avg=65.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
        )
    ]
    assert kpi.compute_burner_era(daily, _era_segments(), "2026-02-01", "2025-12-31") is None


def test_compute_burner_era_capped_at_s3_end() -> None:
    """The era ends at the S3 end even if the window runs further."""
    daily = _era_daily()
    # add burn days AFTER the S3 end (10.11.2026): they must be excluded
    daily = daily + [
        kpi.DayAgg(
            day=f"2026-11-{d:02d}",
            n_samples=100,
            raw_hours=10.0,
            eff_hours=10.0,
            cold30_hours=0.0,
            a5_max=70.0,
            a5_avg=65.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
        )
        for d in (10, 11)
    ]
    era = kpi.compute_burner_era(daily, _era_segments(), "2026-02-01", "2026-11-11", failures=[])
    assert era is not None
    assert era.end == "2026-11-09"
    assert era.eff_hours == pytest.approx(25.0)  # the two 10/11.11 days excluded
    assert [m["month"] for m in era.months] == ["2026-02", "2026-09"]


def test_end_to_end_synthetic_era_and_override_keys(synthetic: dict[str, object]) -> None:
    payload = run_analysis(synthetic)
    # burner_era block present; era capped at the synthetic S3 end
    # (S3 end 2031-01-08 == window end here)
    era = payload["burner_era"]
    assert era is not None
    assert era["start"] == "2026-02-01"
    assert era["end"] == "2031-01-08"
    # Q-127 scenario keys (the old A1 variant keys are gone)
    assert set(era["s3_rate"]) >= {"rate_l_per_day", "oil_days", "litres", "l_per_h", "uncertainty"}
    assert set(era["sub_split"]) >= {
        "s3a_rate_l_per_day",
        "split_days",
        "s3b_litres",
        "litres",
        "l_per_h",
    }
    # the synthetic S3b (mini segment) spans the 2031 normal days:
    # 4 x 11.4 h eff = 45.6 h over 60 L
    mini = era["mini_segment"]
    assert mini["start"] == "2030-09-01" and mini["end"] == "2031-01-08"
    assert mini["eff_hours"] == pytest.approx(45.6, abs=0.01)
    assert mini["l_per_h"] == pytest.approx(60.0 / 45.6, abs=1e-3)
    # the failure windows carry the override fields
    for w in payload["failures"]["windows"]:
        assert w["overridden_days"] == 0 and w["note"] == ""
    for f in payload["failures"]["days"]:
        assert f["overridden"] is False


# ---------------------------------------------------------------------------
# v0.6-D: L/Heizgradtag (HDD-15), um Holz-Anteil korrigiert
# ---------------------------------------------------------------------------
def test_dayagg_v06_defaults() -> None:
    """The new v0.6-D day fields carry safe defaults (no behaviour change
    for aggregate sets built before the field existed)."""
    d = kpi.DayAgg(
        day="2030-01-01",
        n_samples=0,
        raw_hours=0.0,
        eff_hours=0.0,
        cold30_hours=0.0,
        a5_max=0.0,
        a5_avg=0.0,
        ext_avg=0.0,
        wood_hours=0.0,
        o6_hours=0.0,
    )
    assert d.oel_heiz_hours == 0.0
    assert d.l_hdd15 == 0.0
    assert d.oel_heiz_share is None
    assert d.wood_primary is False


def test_load_daily_v06_fields(synthetic: dict[str, object]) -> None:
    """load_daily computes the v0.6-D day fields from the 10 s grid."""
    globs = kpi.DEFAULT_PARQUET_GLOBS
    con = kpi.make_connection()
    try:
        daily = kpi.load_daily(con, globs, str(synthetic["start"]), str(synthetic["end"]), 30.0)
    finally:
        con.close()
    by_day = {d.day: d for d in daily}
    # synthetic ext_avg = 5 deg C -> daily HDD-15 = 10; a9 = 50 deg C (< 65)
    # -> no wood hours; o6 channel all zero -> o6_eff = 0 -> oel_heiz = eff
    n = by_day["2030-01-10"]
    assert n.l_hdd15 == pytest.approx(10.0)
    assert n.oel_heiz_hours == pytest.approx(n.eff_hours)
    assert n.oel_heiz_share == pytest.approx(1.0)  # all hours are oil-heating
    assert n.wood_primary is False
    # failure day: eff = 0 -> oel_heiz 0, no share, not wood-primary
    f = by_day["2030-01-14"]
    assert f.oel_heiz_hours == 0.0
    assert f.oel_heiz_share is None
    assert f.wood_primary is False
    # afterglow day: raw = eff = 0 -> share None
    g = by_day["2030-01-20"]
    assert g.oel_heiz_hours == 0.0 and g.oel_heiz_share is None


def _v06_daily() -> list[kpi.DayAgg]:
    """4 days Jan-2030: day 01 (S start, excl. from KPI span), days 02/03
    interior, day 04 end (incl.).  Day 04 is wood-primary (wood 6 h >
    oil-heating 2 h)."""

    def mk(day: str, oel: float, wood: float, hdd: float) -> kpi.DayAgg:
        return kpi.DayAgg(
            day=day,
            n_samples=100,
            raw_hours=oel,
            eff_hours=oel,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=15.0 - hdd,
            wood_hours=wood,
            o6_hours=0.0,
            oel_heiz_hours=oel,
            l_hdd15=hdd,
            oel_heiz_share=(oel / (oel + wood) if (oel + wood) > 0 else None),
            wood_primary=wood > oel,
        )

    return [
        mk("2030-01-05", 10.0, 0.0, 5.0),  # outside the KPI span (exclusive start)
        mk("2030-01-06", 4.0, 2.0, 10.0),
        mk("2030-01-07", 4.0, 2.0, 10.0),
        mk("2030-01-08", 2.0, 6.0, 5.0),  # wood-primary day
    ]


def test_segment_kpi_rows_formula() -> None:
    """v0.6-D segment KPI: exclusive-start span, raw + corrected + shares,
    S3a (sub-segment) excluded, no band for exact segments."""
    seg = kpi.TankSegment(
        label="S1", start="2030-01-05", end="2030-01-08", litres=100.0, days=3, usable=True
    )
    sub = kpi.TankSegment(
        label="S3a",
        start="2030-01-05",
        end="2030-01-08",
        litres=100.0,
        days=3,
        usable=True,
        top_level=False,
    )
    rows = kpi.segment_kpi_rows([seg, sub], _v06_daily())
    assert [r["label"] for r in rows] == ["S1"]  # S3a never enters the KPI
    r = rows[0]
    # span = 01-06 .. 01-08 (start exclusive): hdd 10+10+5 = 25, oel 10 h,
    # wood 10 h -> oel share 10/20 = 0.5, wood share 50 %
    assert r["hdd15"] == pytest.approx(25.0)
    assert r["oel_heiz_h"] == pytest.approx(10.0)
    assert r["wood_h"] == pytest.approx(10.0)
    assert r["wood_primary_days"] == 1
    assert r["oel_heiz_share"] == pytest.approx(0.5, abs=1e-3)
    assert r["holz_share_pct"] == pytest.approx(50.0, abs=0.1)
    assert r["l_hdd15"] == pytest.approx(100.0 / 25.0)
    # corrected = litres / (hdd * (1 - holz_share)) = 100 / (25 * 0.5) = 8.0
    assert r["l_hdd15_korr"] == pytest.approx(8.0, abs=1e-3)
    # exact segment: no uncertainty band
    assert r["l_hdd15_korr_band"] is None
    assert r["geschätzt"] is False


def test_segment_kpi_rows_degenerate_and_gauge_band() -> None:
    """hdd = 0 or oel-heating = 0 -> no row; gauge segments carry the
    +/- uncertainty band THROUGH the wood correction."""
    daily = [
        kpi.DayAgg(
            day="2030-07-01",
            n_samples=100,
            raw_hours=2.0,
            eff_hours=2.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=20.0,  # no heating demand -> hdd 0
            wood_hours=0.0,
            o6_hours=0.0,
            oel_heiz_hours=2.0,
            l_hdd15=0.0,
        )
    ]
    seg = kpi.TankSegment(
        start="2030-06-30", end="2030-07-02", litres=50.0, days=2, usable=True
    )
    assert kpi.segment_kpi_rows([seg], daily) == []
    # gauge band: (litres -/- uncertainty) / hdd, divided by the oil share
    assert kpi._kpi_gauge_band_hdd(240.0, 175.5, 300.0, None) == [
        0.0,
        kpi.round3(540.0 / 175.5),  # lower bound floored at 0
    ]
    assert kpi._kpi_gauge_band_hdd(100.0, 25.0, 30.0, 0.5) == [5.6, 10.4]
    assert kpi._kpi_gauge_band_hdd(10.0, 0.0, 30.0, 0.5) == [0.0, 0.0]


def test_season_kpi_rows() -> None:
    """v0.6-D season KPI: day-share litres of the covering segment,
    full-season hour basis, wood share + correction, partial flag,
    seasons without tank coverage or without HDD are not listed."""
    daily = [
        # two days inside season 2029/30, one day inside 2030/31
        kpi.DayAgg(
            day="2030-01-10",
            n_samples=100,
            raw_hours=4.0,
            eff_hours=4.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
            oel_heiz_hours=4.0,
            l_hdd15=10.0,
        ),
        kpi.DayAgg(
            day="2030-01-11",
            n_samples=100,
            raw_hours=4.0,
            eff_hours=4.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=0.0,
            o6_hours=0.0,
            oel_heiz_hours=4.0,
            l_hdd15=10.0,
        ),
        kpi.DayAgg(
            day="2030-11-15",
            n_samples=100,
            raw_hours=3.0,
            eff_hours=3.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=5.0,
            wood_hours=2.0,
            o6_hours=0.0,
            oel_heiz_hours=3.0,
            l_hdd15=10.0,
        ),
        # summer day: keeps a segment's summer span HDD-free
        kpi.DayAgg(
            day="2030-07-01",
            n_samples=100,
            raw_hours=1.0,
            eff_hours=1.0,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=25.0,
            wood_hours=0.0,
            o6_hours=0.0,
            oel_heiz_hours=1.0,
            l_hdd15=0.0,
        ),
    ]
    seg = kpi.TankSegment(
        start="2030-01-05", end="2030-01-14", litres=500.0, days=9, usable=True
    )
    summer_seg = kpi.TankSegment(
        start="2030-06-15", end="2030-07-15", litres=50.0, days=30, usable=True
    )
    rows = kpi.season_kpi_rows(daily, [seg, summer_seg])
    # only the season with both litres and HDD is listed:
    # 2030/31 has the Nov-15 day but no segment litres -> no row
    assert [r["season"] for r in rows] == ["2029/30"]
    r = rows[0]
    # litres = day-share over the segment span inside the season:
    # 10 days (01-05 .. 01-14) x 500/9
    assert r["litres_season"] == pytest.approx(10.0 * 500.0 / 9.0, abs=0.01)
    assert r["days_covered"] == 10 and r["is_partial"] is True
    # hours/HDD over the season span: only the two Jan-2030 days fall in
    # 2029/30 (the Nov-2030 day belongs to season 2030/31)
    assert r["hdd15"] == pytest.approx(20.0)
    assert r["oel_heiz_h"] == pytest.approx(8.0)
    assert r["wood_h"] == pytest.approx(0.0)
    assert r["oel_heiz_share"] == pytest.approx(1.0, abs=1e-3)
    assert r["holz_share_pct"] == 0.0
    lit = 10.0 * 500.0 / 9.0
    assert r["l_hdd15"] == pytest.approx(lit / 20.0, abs=1e-3)
    assert r["l_hdd15_korr"] == pytest.approx(lit / 20.0, abs=1e-3)  # no wood
    assert r["gauge"] is False
    # the summer-only segment contributes litres but no HDD -> no row
    assert all(r["season"] != "2030/31" for r in rows)


def test_month_kpi_rows_per_day_attribution() -> None:
    """v0.6-D month KPI: per-day segment attribution (boundary months),
    uncovered days flagged, degenerate summer months (HDD < 5) skipped,
    wood-primary days counted, full-period wood share."""
    seg_a = kpi.TankSegment(
        label="A", start="2030-01-01", end="2030-01-15", litres=300.0, days=14, usable=True
    )
    seg_b = kpi.TankSegment(
        label="B", start="2030-01-15", end="2030-02-15", litres=300.0, days=31, usable=True
    )

    def mk(day: str, oel: float, wood: float, ext: float = 5.0) -> kpi.DayAgg:
        return kpi.DayAgg(
            day=day,
            n_samples=100,
            raw_hours=oel,
            eff_hours=oel,
            cold30_hours=0.0,
            a5_max=75.0,
            a5_avg=60.0,
            ext_avg=ext,
            wood_hours=wood,
            o6_hours=0.0,
            oel_heiz_hours=oel,
            l_hdd15=kpi.daily_hdd15(ext),
            oel_heiz_share=(oel / (oel + wood) if (oel + wood) > 0 else None),
            wood_primary=wood > oel,
        )

    jan = [mk(f"2030-01-{d:02d}", 2.0, 0.0) for d in range(1, 32)]
    feb = [mk(f"2030-02-{d:02d}", 1.0, 5.0 if d == 3 else 0.0) for d in range(1, 6)]
    aug = [mk(f"2030-08-{d:02d}", 2.0, 0.0, ext=16.0) for d in range(1, 4)]  # hdd < 5
    dec29 = [mk("2029-12-01", 2.0, 0.0)]  # no segment coverage at all
    rows = {r["month"]: r for r in kpi.month_kpi_rows(jan + feb + aug + dec29, [seg_a, seg_b])}
    assert set(rows) == {"2030-01", "2030-02"}
    r1 = rows["2030-01"]
    # 01-02 .. 01-15 -> seg A (14 d x 300/14), 01-16 .. 01-31 -> seg B
    # (16 d x 300/31); 01-01 uncovered (seg A start day exclusive)
    assert r1["estimated_days"] == 1 and r1["month_days"] == 31
    assert r1["estimated"] is True
    assert r1["sources"] == ["A", "B"]
    lit = 300.0 + 16.0 * 300.0 / 31.0
    assert r1["litres"] == pytest.approx(lit, abs=0.01)
    assert r1["hdd15"] == pytest.approx(310.0)
    assert r1["holz_share_pct"] == 0.0
    assert r1["l_hdd15"] == pytest.approx(lit / 310.0, abs=1e-3)
    assert r1["l_hdd15_korr"] == pytest.approx(r1["l_hdd15"], abs=1e-3)  # no wood
    r2 = rows["2030-02"]
    assert r2["sources"] == ["B"]
    lit2 = 5.0 * 300.0 / 31.0
    assert r2["litres"] == pytest.approx(lit2, abs=0.01)
    assert r2["wood_primary_days"] == 1  # 02-03: wood 5 h > oil-heating 1 h
    assert r2["holz_share_pct"] == pytest.approx(100.0 * 5.0 / 10.0, abs=0.1)
    assert r2["l_hdd15_korr"] == pytest.approx(lit2 / (50.0 * 0.5), abs=1e-3)


def test_v06_payload_and_report_synthetic(synthetic: dict[str, object]) -> None:
    """End-to-end: the v06_kpi block (segments/seasons/months) is in the
    JSON payload and the report carries the v0.6 section + October block."""
    payload = run_analysis(synthetic)
    v = payload["v06_kpi"]
    assert set(v) == {"method", "segments", "seasons", "months"}
    assert "HDD-15" in str(v["method"]["kpi"])
    # segments: S3a (sub-segment) and S2 (no data days inside the span)
    # are absent; the three others are present
    assert [r["label"] for r in v["segments"]] == ["S1", "S3", "S3b"]
    # synthetic has no wood hours (a9 = 50 deg C < 65) -> wood share 0 %,
    # corrected KPI == raw KPI
    for r in v["segments"]:
        assert r["holz_share_pct"] == 0.0
        assert r["l_hdd15_korr"] == pytest.approx(r["l_hdd15"], abs=1e-3)
    s1 = next(r for r in v["segments"] if r["label"] == "S1")
    # S1 span 01-06 .. 03-01: synthetic days 01-10, 01-11, 01-14 .. 01-20
    # (9 days) + 02-06/07 = 11 days x 10 HDD
    assert s1["hdd15"] == pytest.approx(110.0, abs=0.1)
    assert s1["l_hdd15"] == pytest.approx(400.0 / 110.0, abs=1e-3)
    s3 = next(r for r in v["segments"] if r["label"] == "S3")
    # S3 span 06-02 .. 01-08: only the 4 x 2031-01 days (05 .. 08)
    assert s3["hdd15"] == pytest.approx(40.0, abs=0.1)
    assert s3["l_hdd15"] == pytest.approx(260.0 / 40.0, abs=1e-3)
    s3b = next(r for r in v["segments"] if r["label"] == "S3b")
    # S3b span 09-02 .. 01-08: the same 4 days x 10 HDD, 60 L
    assert s3b["l_hdd15"] == pytest.approx(60.0 / 40.0, abs=1e-3)
    assert s3b["geschätzt"] is True and s3b["gauge"] is True
    assert s3b["l_hdd15_korr_band"] is not None  # gauge mini segment
    # seasons: two covered seasons, both partial, korr == raw (no wood)
    assert [r["season"] for r in v["seasons"]] == ["2029/30", "2030/31"]
    for r in v["seasons"]:
        assert r["is_partial"] is True
        assert r["l_hdd15_korr"] == pytest.approx(r["l_hdd15"], abs=1e-3)
    # months: only months with HDD >= 5 and segment coverage
    assert [r["month"] for r in v["months"]] == ["2030-01", "2030-02", "2031-01"]
    for r in v["months"]:
        assert r["holz_share_pct"] == 0.0
        assert r["l_hdd15_korr"] == pytest.approx(r["l_hdd15"], abs=1e-3)
    # report carries the v0.6 section + the (empty) October example block
    out = Path(str(synthetic["out"]))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## v0.6: L/Heizgradtag" in report
    assert "L/HDD korr." in report
    assert "v0.6-E: Beispiel Okt-2026" in report
    assert "Keine Oktober-Monate im Datenfenster" in report


@pytest.mark.slow
def test_real_v06_kpi_numbers() -> None:
    """v0.6-D Kernzahlen (Real-Data, aus dem CLI-Run 2026-11-12 abgelesen,
    NICHT geraten): Segmente, Saisons, Okt-2026-Beispiel."""
    _require_real_data()
    payload = kpi.run(
        REAL_START,
        REAL_END,
        30.0,
        Path("tmp/oel_kpi_smoke"),
        Path("tmp/tank_zollstock_2015_2026.csv"),
    )
    v = payload["v06_kpi"]
    segs = {r["label"]: r for r in v["segments"]}
    assert set(segs) == {"S1", "S2", "S3", "S3b"}
    # S1/S2 exakt (kein Band), S3/S3b mit Gauge-Band
    assert segs["S1"]["l_hdd15_korr_band"] is None
    assert segs["S1"]["l_hdd15"] == pytest.approx(1.923, abs=0.01)
    assert segs["S1"]["l_hdd15_korr"] == pytest.approx(2.166, abs=0.01)
    assert segs["S1"]["holz_share_pct"] == pytest.approx(11.2, abs=0.1)
    assert segs["S2"]["l_hdd15"] == pytest.approx(1.815, abs=0.01)
    assert segs["S2"]["l_hdd15_korr"] == pytest.approx(1.926, abs=0.01)
    assert segs["S2"]["holz_share_pct"] == pytest.approx(5.7, abs=0.1)
    assert segs["S3"]["l_hdd15"] == pytest.approx(1.587, abs=0.01)
    assert segs["S3"]["l_hdd15_korr"] == pytest.approx(2.020, abs=0.01)
    assert segs["S3"]["l_hdd15_korr_band"] == [1.793, 2.247]
    assert segs["S3b"]["l_hdd15"] == pytest.approx(1.367, abs=0.01)
    assert segs["S3b"]["l_hdd15_korr"] == pytest.approx(5.917, abs=0.01)
    assert segs["S3b"]["holz_share_pct"] == pytest.approx(76.9, abs=0.1)
    # Saisons: n = 4 (Q-127), 2026/27 Teil-Saison
    rows = v["seasons"]
    assert [r["season"] for r in rows] == ["2023/24", "2024/25", "2025/26", "2026/27"]
    seas = {r["season"]: r for r in rows}
    assert [r["is_partial"] for r in rows] == [False, False, False, True]
    assert seas["2023/24"]["l_hdd15"] == pytest.approx(0.862, abs=0.01)
    assert seas["2023/24"]["l_hdd15_korr"] == pytest.approx(0.991, abs=0.01)
    assert seas["2024/25"]["l_hdd15_korr"] == pytest.approx(1.120, abs=0.01)
    assert seas["2025/26"]["l_hdd15_korr"] == pytest.approx(1.100, abs=0.01)
    # 2026/27 is the RUNNING season: its values drift with every new data
    # day (2026-11-12: holz 84.7 %, korr 10.23) -> range-based assertion
    assert seas["2026/27"]["holz_share_pct"] > 80.0
    assert 8.0 <= seas["2026/27"]["l_hdd15_korr"] <= 12.0
    # Monate: keine degenerierten Sommer-Monate (HDD < 5), Okt-2026 korrekt
    mons = {r["month"]: r for r in v["months"]}
    for mon in ("2025-06", "2025-07", "2025-08", "2026-06", "2026-07", "2026-08"):
        assert mon not in mons
    o26 = mons["2026-10"]
    assert o26["hdd15"] == pytest.approx(96.2, abs=0.1)
    assert o26["holz_share_pct"] == pytest.approx(76.4, abs=0.1)
    assert o26["wood_primary_days"] == 13
    assert o26["l_hdd15"] == pytest.approx(2.251, abs=0.01)
    assert o26["l_hdd15_korr"] == pytest.approx(9.54, abs=0.01)
    o25 = mons["2025-10"]
    assert o25["l_hdd15"] == pytest.approx(2.31, abs=0.01)
    assert o25["l_hdd15_korr"] == pytest.approx(3.067, abs=0.01)
    # report: v0.6-Section + Okt-2026-Beispiel mit allen drei Vergleichsmonaten
    out = Path("tmp/oel_kpi_smoke")
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## v0.6: L/Heizgradtag" in report
    for mon in ("2026-10", "2025-10", "2021-10"):
        assert mon in report
