"""Tests: Bias-Profil (synthetische Sensor- + Grid-Daten, kein Netzwerk).

Deckt: Saison-Zuordnung (DJF/MAM/JJA/SON inkl. Dezember-Split), Median-Offset,
OLS-Drift (K/Tag ab Saisonstart), grid_coverage, save/load round-trip,
leeres Profil (keine Daten), Out-of-Range-Ausschluss und ``prepare_con``.

Synthetischer Durchlauf (``test_synthetic_median_and_drift``):
- Grid-T = 10.0 konstant (Wetter-Roh-Cache, hfa-Namespace, 24 h/Tag),
- Sensor = 10.0 + resid(day), resid je Monat:
  - 2022-08 (JJA): 2.0 konstant  → offset 2.0, drift 0.
  - 2023-01 (DJF): 0.5 + 0.1·(d−1) → offset (Median) = 2.0, drift = 0.1 K/Tag.
  - 2023-11 (SON): d−1            → offset = 14.5, drift = 1.0 K/Tag.
Nur die 02:00-Uhr je Tag trägt Sensor-Rows (hourly_agg) → 1 Pairstag je
Kalendertag (02:00 Berlin bleibt in UTC im selben Kalendertag); Grid-Rows
fehlen für alle anderen Stunden (kein Sensor).
"""
import calendar
import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from analysis.model.m1 import bias, config
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.features import bias_terms, build_hour_grid


def _write_wx_cache(cache_dir: Path, year: int, month: int, base_t: float = 10.0) -> None:
    """Open-Meteo-Monats-Chunk (Roh-Cache-Format, 24 h × n Tage).

    Namespace: ``era5`` für Monate < 2022-11 (ERA5-Reanalyse), ``hfa`` ab 2022-11
    (ICON-D2 Historical-Forecast) — passend zu ``load_wx_long``.
    """
    last = calendar.monthrange(year, month)[1]
    times = [
        f"{year:04d}-{month:02d}-{d:02d}T{h:02d}:00"
        for d in range(1, last + 1)
        for h in range(24)
    ]
    data = {
        "latitude": 51.38,
        "longitude": 7.77,
        "hourly": {"time": times, "temperature_2m": [base_t] * len(times)},
        "daily": {
            "time": [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, last + 1)],
            "sunrise": ["00:00"] * last,
            "sunset": ["23:00"] * last,
        },
    }
    ns = "era5" if (year, month) < (2022, 11) else "hfa"
    p = cache_dir / ns
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{year:04d}-{month:02d}.json").write_text(json.dumps(data), encoding="utf-8")


def _register_fixture(
    con: duckdb.DuckDBPyConnection,
    cache_dir: Path,
    months: list[tuple[int, int]],
    resid_fn,
    base_t: float = 10.0,
) -> None:
    """``hour_grid`` (komplettes Raster) + ``hourly_agg`` (02:00 je Tag) + Cache.

    Sensor-Std = 02:00 Berlin (→ 00:00 UTC im Sommer / 01:00 UTC im Winter):
    die UTC-Stunde liegt damit IMMER im selben Wetter-Cache-Monat wie der
    Berliner Kalendertag (00:00/01:00 Berlin lägen im Winter 22:00/23:00 UTC
    im Vortag bzw. Vormonat).
    """
    buckets, exts = [], []
    for y, m in months:
        for d in range(1, 32):
            try:
                day = date(y, m, d)
            except ValueError:
                break
            h = datetime(y, m, d, 2, 0, tzinfo=BERLIN)
            buckets.append(h.strftime("%Y-%m-%d %H") + ":00")
            exts.append(base_t + resid_fn(day))
    (ly, lm) = months[-1]
    grid = build_hour_grid(
        datetime(months[0][0], months[0][1], 1, tzinfo=BERLIN),
        datetime(ly, lm, calendar.monthrange(ly, lm)[1], 23, 0, tzinfo=BERLIN),
    )
    con.register("hour_grid", grid)
    con.execute(
        "CREATE OR REPLACE TABLE hourly_agg AS SELECT * FROM (VALUES "
        + ", ".join(f"('{b}', {e})" for b, e in zip(buckets, exts, strict=True))
        + ") v(ts_bucket, ext)"
    )
    for y, m in months:
        _write_wx_cache(cache_dir, y, m, base_t=base_t)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    yield c
    c.close()


def _cfg(tmp_path: Path) -> config.BuildConfig:
    """BuildConfig: hfa_months = 2022-11 → 2023-11 (deckt alle Test-Monate)."""
    return config.BuildConfig(wx_cache_dir=tmp_path, fetch_end="2023-11-30")


class TestSeasonOf:
    def test_djf(self):
        assert bias._season_of(12) == "DJF"
        assert bias._season_of(1) == "DJF"
        assert bias._season_of(2) == "DJF"

    def test_mam(self):
        assert bias._season_of(3) == "MAM"
        assert bias._season_of(4) == "MAM"
        assert bias._season_of(5) == "MAM"

    def test_jja(self):
        assert bias._season_of(6) == "JJA"
        assert bias._season_of(7) == "JJA"
        assert bias._season_of(8) == "JJA"

    def test_son(self):
        assert bias._season_of(9) == "SON"
        assert bias._season_of(10) == "SON"
        assert bias._season_of(11) == "SON"

    def test_season_start(self):

        assert bias._season_start(2020, 12) == datetime(2019, 12, 1, tzinfo=UTC)
        assert bias._season_start(2020, 1) == datetime(2020, 1, 1, tzinfo=UTC)
        assert bias._season_start(2020, 2) == datetime(2020, 1, 1, tzinfo=UTC)
        assert bias._season_start(2020, 3) == datetime(2020, 3, 1, tzinfo=UTC)
        assert bias._season_start(2020, 5) == datetime(2020, 3, 1, tzinfo=UTC)
        assert bias._season_start(2020, 6) == datetime(2020, 6, 1, tzinfo=UTC)
        assert bias._season_start(2020, 8) == datetime(2020, 6, 1, tzinfo=UTC)
        assert bias._season_start(2020, 9) == datetime(2020, 9, 1, tzinfo=UTC)
        assert bias._season_start(2020, 11) == datetime(2020, 9, 1, tzinfo=UTC)


class TestComputeBias:
    def test_synthetic_median_and_drift(self, con, tmp_path):
        """Deterministischer Durchlauf: Median-Offset + OLS-Drift je Saison."""
        months = [(2022, 8), (2023, 1), (2023, 11)]

        def resid(day: date) -> float:
            if day.month == 8:
                return 2.0
            if day.month == 1:
                return 0.5 + 0.1 * (day.day - 1)
            return float(day.day - 1)

        _register_fixture(con, tmp_path, months, resid)
        result = bias.compute_bias(con, _cfg(tmp_path))
        # JJA (2022-08, 31 Tage): konstant 2.0 → offset 2.0, drift 0
        assert abs(result.jja["offset_k"] - 2.0) < 1e-9
        assert abs(result.jja["drift_k_per_day"]) < 1e-9
        assert result.jja["n_hours"] == 31
        # DJF (2023-01, 31 Tage): resid = 0.5 + 0.1·(d−1) ∈ [0.5, 3.5]
        # → Median = 2.0, OLS-Steigung = 0.1 K/Tag
        assert abs(result.djf["offset_k"] - 2.0) < 1e-9
        assert abs(result.djf["drift_k_per_day"] - 0.1) < 1e-9
        assert result.djf["n_hours"] == 31
        # SON (2023-11, 30 Tage): resid = d−1 ∈ {0,1,…,29} (30 Werte, gerade)
        # → Median = Mittel der beiden mittleren = (14 + 15)/2 = 14.5
        assert abs(result.son["offset_k"] - 14.5) < 1e-9
        assert abs(result.son["drift_k_per_day"] - 1.0) < 1e-9
        assert result.son["n_hours"] == 30
        # MAM: keine Daten
        assert result.mam["n_hours"] == 0
        assert result.mam["offset_k"] == 0.0
        assert result.n_hours_total == 92
        # grid_coverage: 31/24×90 etc. → hier > 0 und ≤ 1
        assert 0.0 < result.grid_coverage["JJA"] <= 1.0
        assert 0.0 < result.grid_coverage["DJF"] <= 1.0
        assert 0.0 < result.grid_coverage["SON"] <= 1.0
        assert 0.0 <= result.grid_coverage["MAM"] <= 1.0

    def test_constant_resid_zero_drift(self, con, tmp_path):
        """Konstantes Residuum → Drift exakt 0 (OLS-Formel), negatives Offset."""
        months = [(2022, 6)]
        _register_fixture(con, tmp_path, months, lambda d: -1.5)
        result = bias.compute_bias(con, _cfg(tmp_path))
        assert abs(result.jja["offset_k"] + 1.5) < 1e-9
        assert abs(result.jja["drift_k_per_day"]) < 1e-9
        assert result.jja["n_hours"] == 30

    def test_empty_profile(self, con, tmp_path):
        """Keine Cache-Dateien → alle n_hours = 0, offset = 0.0, drift = 0.0."""
        h0 = datetime(2020, 8, 1, tzinfo=BERLIN)
        h1 = datetime(2020, 8, 2, tzinfo=BERLIN)
        con.register("hour_grid", build_hour_grid(h0, h1))
        con.execute(
            "CREATE OR REPLACE TABLE hourly_agg AS SELECT "
            "'2020-08-01 00:00' AS ts_bucket, 10.0 AS ext"
        )
        result = bias.compute_bias(con, config.BuildConfig(wx_cache_dir=tmp_path / "leer"))
        for s in config.BIAS_SEASONS:
            p = getattr(result, s.lower())
            assert p["n_hours"] == 0
            assert p["offset_k"] == 0.0
            assert p["drift_k_per_day"] == 0.0
        assert result.n_hours_total == 0

    def test_out_of_range_sensor_excluded(self, con, tmp_path):
        """Sensor-Werte außerhalb [−40, 60] K werden ausgeschlossen (Fehler-Samples)."""
        _register_fixture(con, tmp_path, [(2022, 8)], lambda d: 2.0, base_t=10.0)
        con.execute("DELETE FROM hourly_agg WHERE ts_bucket = '2022-08-15 02:00'")
        con.execute("INSERT INTO hourly_agg VALUES ('2022-08-15 02:00', 99.0)")
        result = bias.compute_bias(con, _cfg(tmp_path))
        # 31 Tage − 1 (99.0 ausgeschlossen) = 30 gültige
        assert result.jja["n_hours"] == 30

    def test_bias_result_as_profile_keys(self):
        r = bias.BiasResult(
            djf={"offset_k": 1.0, "drift_k_per_day": 0.0, "n_hours": 10, "season_hours": 100},
            mam={"offset_k": 2.0, "drift_k_per_day": 0.0, "n_hours": 10, "season_hours": 100},
            jja={"offset_k": 3.0, "drift_k_per_day": 0.0, "n_hours": 10, "season_hours": 100},
            son={"offset_k": 4.0, "drift_k_per_day": 0.0, "n_hours": 10, "season_hours": 100},
            n_hours_total=40,
            grid_coverage={"DJF": 0.5, "MAM": 0.5, "JJA": 0.5, "SON": 0.5},
        )
        prof = r.as_profile()
        assert set(prof.keys()) == {"DJF", "MAM", "JJA", "SON"}
        assert prof["JJA"]["offset_k"] == 3.0


class TestSaveLoadProfile:
    def test_roundtrip(self, tmp_path):
        r = bias.BiasResult(
            djf={"offset_k": 1.5, "drift_k_per_day": 0.001, "n_hours": 100, "season_hours": 200},
            mam={"offset_k": 2.5, "drift_k_per_day": -0.002, "n_hours": 200, "season_hours": 400},
            jja={"offset_k": 3.5, "drift_k_per_day": 0.0, "n_hours": 300, "season_hours": 600},
            son={"offset_k": 4.5, "drift_k_per_day": 0.003, "n_hours": 400, "season_hours": 800},
            n_hours_total=1000,
            grid_coverage={"DJF": 0.5, "MAM": 0.5, "JJA": 0.5, "SON": 0.5},
        )
        p = tmp_path / "bias_profile.json"
        bias.save_profile(r, p)
        assert p.exists()
        with p.open() as f:
            data = json.load(f)
        assert data["n_hours_total"] == 1000
        assert data["profile"]["JJA"]["offset_k"] == 3.5
        loaded = bias.load_profile(p)
        assert loaded is not None
        assert loaded["JJA"]["offset_k"] == 3.5
        assert loaded["DJF"]["drift_k_per_day"] == 0.001

    def test_load_missing(self, tmp_path):
        assert bias.load_profile(tmp_path / "nonexistent.json") is None

    def test_load_invalid_json(self, tmp_path):
        p = tmp_path / "bias_profile.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert bias.load_profile(p) is None

    def test_load_missing_profile_key(self, tmp_path):
        p = tmp_path / "bias_profile.json"
        p.write_text(json.dumps({"other": 1}), encoding="utf-8")
        assert bias.load_profile(p) is None


class TestPrepareCon:
    def test_prepare_con_creates_views(self, con):
        """prepare_con: hour_grid + hourly_agg vorhanden (echte Roh-Quellen, read-only)."""
        cfg = config.BuildConfig()
        bias.prepare_con(con, cfg)
        names = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert {"hour_grid", "hourly_agg", "src"} <= names
        cols = {c[0] for c in con.execute("DESCRIBE SELECT * FROM hourly_agg").fetchall()}
        for needed in ("ts_bucket", "ext", "hz_vl", "offset_kell", "heat_min_kell",
                       "oel_burner_on", "ras_divergence_kell", "n_rows", "source"):
            assert needed in cols, f"fehlt: {needed}"
        n = con.execute("SELECT count(*) FROM hourly_agg").fetchone()[0]
        assert n > 50000  # ~53k Kalenderstunden

    def test_compute_bias_after_prepare(self, con, tmp_path):
        """compute_bias nach prepare_con ohne Wetter-Cache: läuft durch (0 Stunden).

        Isoliertes wx_cache (tmp_path), damit der laufende Produktion-Backfill
        (``data/features/wx_cache``) das Ergebnis nicht verfälscht.
        """
        cfg = config.BuildConfig(wx_cache_dir=tmp_path / "leer-cache")
        bias.prepare_con(con, cfg)
        result = bias.compute_bias(con, cfg)
        assert result.n_hours_total == 0
        assert all(p["n_hours"] == 0 for p in result.as_profile().values())


class TestBiasTermsSql:
    """features.bias_terms: SQL-Ausdruck (Roh-Grid-T + saisonales Profil)."""

    def test_no_profile_identity(self):
        terms = bias_terms(None, 24)
        assert len(terms) == 24
        assert terms[0] == "wx_t0_t AS wx_t0_t_bias"
        assert terms[23] == "wx_t23_t AS wx_t23_t_bias"

    def test_with_profile_case_branches(self):
        prof = {
            "DJF": {"offset_k": 1.0, "drift_k_per_day": 0.001},
            "MAM": {"offset_k": 2.0, "drift_k_per_day": 0.0},
            "JJA": {"offset_k": 3.0, "drift_k_per_day": -0.002},
            "SON": {"offset_k": 4.0, "drift_k_per_day": 0.003},
        }
        terms = bias_terms(prof, 24)
        assert len(terms) == 24
        for t in terms:
            assert "AS wx_t" in t
            for s in ("DJF", "MAM", "JJA", "SON"):
                assert f"'{s}'" in t
            assert "datediff('day'" in t

    def test_bias_values_applied(self, con):
        """End-to-end: Bias-Offset wird korrekt auf Grid-T angewendet (DuckDB)."""
        prof = {
            "DJF": {"offset_k": 1.0, "drift_k_per_day": 0.0},
            "MAM": {"offset_k": 0.0, "drift_k_per_day": 0.0},
            "JJA": {"offset_k": 0.0, "drift_k_per_day": 0.0},
            "SON": {"offset_k": 0.0, "drift_k_per_day": 0.0},
        }
        terms = bias_terms(prof, 3)
        con.execute("CREATE TABLE g AS SELECT TIMESTAMP '2024-01-15 10:00' ts")
        con.execute("CREATE TABLE wx AS SELECT 5.0 AS wx_t0_t, 6.0 AS wx_t1_t, 7.0 AS wx_t2_t")
        row = con.execute(f"SELECT {', '.join(terms)} FROM g CROSS JOIN wx").fetchone()
        assert row[0] == 6.0  # 5.0 + 1.0 (DJF-Offset)
        assert row[1] == 7.0
        assert row[2] == 8.0

    def test_bias_null_grid_is_null(self, con):
        """NULL-Grid-T → NULL-bias (kein NaN)."""
        prof = {s: {"offset_k": 1.0, "drift_k_per_day": 0.0} for s in config.BIAS_SEASONS}
        terms = bias_terms(prof, 1)
        con.execute("CREATE TABLE g AS SELECT TIMESTAMP '2024-01-15 10:00' ts")
        con.execute("CREATE TABLE wx AS SELECT NULL::DOUBLE AS wx_t0_t")
        row = con.execute(f"SELECT {terms[0]} FROM g CROSS JOIN wx").fetchone()
        assert row[0] is None
