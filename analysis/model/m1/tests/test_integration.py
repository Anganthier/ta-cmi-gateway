"""Tests: Feature-Assemblierung (Integrations-Tests, synthetische Quellen).

Deckt die kompletten ``assemble_features`` + ``store_features``-Pfade:
- synthetische 10-s-Quellen (winsol- + cmi-ähnlich, kein Overlap →
  Source-Flag-Wechsel),
- synthetischer Wetter-Roh-Cache (hfa-Namespace),
- ``hour_grid`` + ``hourly_agg`` + ``wx_wide`` + Sun + Bias-Terms + Lags,
- Gap-Flags, Metadaten-Block, ``_meta.json``,
- m1.1: RAS-Maske vor ``RAS_INSTALL_TS`` (inkl. Lags), ``ras_age_*``, ``{feat}_last``,
  ``report.m1_1_checks``,
- m1.2: TA-Sentinel-Maske im Store, ``hzrkl_*`` (inkl. Lags, a14/a15-Vertauschung),
  ``report.m1_2_checks`` / ``report.sentinel_counts``.

Zusätzlich: ``report.coverage`` / ``report.feature_gaps`` /
``report.spot_checks`` auf dem fertigen (synthetischen) Feature-Store +
``build._git_tag``/``_now_iso``/``main``-Argumentparsing.
"""
import calendar
import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import build, config, features, report, resample
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.features import (
    LAG_COLUMNS,
    assemble_features,
    store_features,
    store_meta,
)


def make_source_rows(hour_start: datetime, n_rows: int, base: float = 20.0,
                     *, burner_vals=None) -> pa.Table:
    """Synthetische 10-s-Rows (alle SOURCE_COLUMNS, deterministisch)."""
    ts = [(hour_start + timedelta(seconds=10 * i)).strftime("%Y-%m-%d %H:%M:%S")
          for i in range(n_rows)]
    t = {"ts": pa.array(ts, type=pa.string())}
    for col in resample.SOURCE_COLUMNS[1:]:
        if col in ("dev1_analog_11", "dev1_analog_12", "dev1_analog_13"):
            # Heiz-Minuten-Zähler: 0→n_rows−1 (stündlicher Reset → negatives
            # Delta über Stunden → ignoriert; innerhalb: n_rows−1 positive Deltas)
            t[col] = pa.array([i for i in range(n_rows)], type=pa.float64())
        elif col == "dev0_digital_05" and burner_vals is not None:
            t[col] = pa.array(burner_vals, type=pa.float64())
        elif col in ("dev0_digital_05", "dev2_digital_01", "dev0_digital_08"):
            t[col] = pa.array([1 if i % 2 == 0 else 0 for i in range(n_rows)],
                              type=pa.float64())
        elif col in ("dev1_analog_01", "dev1_analog_04", "dev1_analog_07"):
            t[col] = pa.array([base + 1.0] * n_rows, type=pa.float64())  # perc
        elif col in ("dev2_analog_10", "dev2_analog_11", "dev2_analog_12"):
            # m1.2 HzRkl Ventil 2 Ergebnis: binär 0/10, 1 von 4 Rows an (Endwert 0)
            t[col] = pa.array([10.0 if i % 4 == 0 else 0.0 for i in range(n_rows)],
                              type=pa.float64())
        elif col in ("dev2_analog_14", "dev2_analog_15"):
            # m1.2 HzRkl Ventil 1: a14 (OG → dg) = base + 2, a15 (EG) = base + 3
            t[col] = pa.array([base + (2.0 if col.endswith("14") else 3.0)] * n_rows,
                              type=pa.float64())
        else:
            t[col] = pa.array([base] * n_rows, type=pa.float64())
    return pa.table(t)


def make_wx_cache(cache_dir: Path, months_era5, months_hfa, base_t: float = 10.0) -> None:
    """Synthetischer Open-Meteo-Roh-Cache (alle 14 Variablen, 24 h/Tag)."""
    entries = [(mo, "era5") for mo in months_era5] + [(mo, "hfa") for mo in months_hfa]
    for (y, m), ns in entries:
        last = calendar.monthrange(y, m)[1]
        times = [f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:00"
                 for d in range(1, last + 1) for h in range(24)]
        data = {
            "hourly": {
                "time": times,
                **{v: [base_t + (i % 24) * 0.1 for i in range(len(times))]
                   for v in config.WX_HOURLY_VARS},
            },
            "daily": {"time": [f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, last + 1)],
                      "sunrise": ["06:00"] * last, "sunset": ["18:00"] * last},
        }
        p = cache_dir / ns
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{y:04d}-{m:02d}.json").write_text(json.dumps(data), encoding="utf-8")


def make_small_sources(root: Path, n_hours: int = 48,
                       start: datetime = None) -> tuple[Path, Path]:
    """zwei kleine Parquet-Quellen (winsol + cmi, kein Overlap).

    winsol: Stunden 0..half−1, cmi: Stunden half..n_hours−1 → Source-Flag-Wechsel.
    """
    start = start or datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
    half = n_hours // 2
    w_rows = pa.concat_tables(
        [make_source_rows(start + timedelta(hours=i), 360) for i in range(half)]
    )
    c_rows = pa.concat_tables(
        [make_source_rows(start + timedelta(hours=half + i), 360) for i in range(half)]
    )
    winsol = root / "winsol"
    cmi = root / "cmi"
    for t, base in ((w_rows, winsol), (c_rows, cmi)):
        p = base / "2024" / "01" / "2024-01.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, p)
    return winsol, cmi


@pytest.fixture
def small_env(tmp_path):
    """Kleine Pipeline-Umgebung: Quellen + Wetter-Cache + BuildConfig (48 h)."""
    start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
    winsol, cmi = make_small_sources(tmp_path / "src", n_hours=48, start=start)
    cache = tmp_path / "wx"
    # 2024-01 liegt im hfa-Ära (≥ 2022-11) → hfa-Namespace
    make_wx_cache(cache, [], [(2024, 1)])
    cfg = config.BuildConfig(
        features_dir=tmp_path / "features",
        wx_cache_dir=cache,
        winsol_pq=winsol,
        cmi_log_pq=cmi,
        fetch_end="2024-01-31",
        first_hour=start,
    )
    return cfg


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    yield c
    c.close()


class TestAssembleFeatures:
    def test_schema_and_rows(self, small_env, con):
        """Komplette Assemblierung: alle Spaltengruppen, korrekte Row-Zahl."""
        cfg = small_env
        bias_profile = {s: {"offset_k": 0.5, "drift_k_per_day": 0.0}
                        for s in config.BIAS_SEASONS}
        tbl = assemble_features(con, cfg, bias_profile, git_tag="testgit",
                                generated_at="2026-01-01T00:00:00")
        cols = set(tbl.column_names)
        for c in ("ts", "hour", "day_of_week", "is_weekend", "is_holiday",
                  "is_dst", "n_rows", "gap", "gap_full", "gap_partial", "source",
                  "mode_known", "offset_known", "data_version", "git_tag",
                  "generated_at", "wx_is_forecast", "sunrise_min", "sunset_min",
                  "o9_switch_margin",
                  "daylight_min"):
            assert c in cols, f"fehlt: {c}"
        for c in LAG_COLUMNS:
            assert c in cols, f"fehlt State: {c}"
        for k in config.LAG_HOURS:
            for c in LAG_COLUMNS:
                assert f"{c}_lag{k}" in cols, f"fehlt Lag: {c}_lag{k}"
        for k in range(config.WX_WINDOW_H):
            for _v, short in config.WX_SHORT.items():
                assert f"wx_t{k}_{short}" in cols, f"fehlt wx: wx_t{k}_{short}"
            # Bias-Korrektur NUR für Grid-T (wx_t{k}_t → wx_t{k}_t_bias)
            assert f"wx_t{k}_t_bias" in cols, f"fehlt bias: wx_t{k}_t_bias"
        assert tbl.num_rows == 48
        # Metadaten-Strings
        assert tbl["data_version"].to_pylist() == ["m1.3"] * 48
        for c in ("hzrkl_kell_rule", "hzrkl_kell_rule_last", "pump_wood_est_on",
                  "wood_fire_on", "oel_burner_on_sum3h", "oel_burner_on_sum24h",
                  "oel_burner_on_since_h"):
            assert c in cols
        assert tbl["git_tag"].to_pylist() == ["testgit"] * 48

    def test_flags_and_values(self, small_env, con):
        """Gap-Flags, Source-Flag, Bias-Korrektur, Lags, Wetter-Fenster."""
        cfg = small_env
        bias_profile = {"DJF": {"offset_k": 1.0, "drift_k_per_day": 0.0},
                        "MAM": {"offset_k": 0.0, "drift_k_per_day": 0.0},
                        "JJA": {"offset_k": 0.0, "drift_k_per_day": 0.0},
                        "SON": {"offset_k": 0.0, "drift_k_per_day": 0.0}}
        tbl = assemble_features(con, cfg, bias_profile, "git", "ts")
        d = tbl.to_pydict()
        i0 = 0  # 2024-01-01 00:00 Berlin (UTC = 2023-12-31 23:00, vor Cache-Start)
        assert d["gap"][i0] == 0 and d["gap_full"][i0] == 0
        assert d["n_rows"][i0] == 360
        assert d["source"][i0] == "winsol_pq"
        assert d["ext"][i0] == 20.0
        assert d["offset_kell"][i0] == 1.0  # perc 21.0 − ist 20.0
        assert d["oel_burner_on"][i0] == 0.5  # 1,0,1,0,…
        assert d["oel_burner_on_sum3h"][i0] == pytest.approx(0.5)
        assert d["oel_burner_on_sum3h"][2] == pytest.approx(1.5)
        assert d["oel_burner_on_since_h"][i0] == 0
        assert d["o9_switch_margin"][i0] == -5.0
        assert d["heat_min_kell"][i0] == 359.0  # 0→359: 359 positive Deltas
        assert d["wx_is_forecast"][i0] == 1  # 2024 ≥ 2022-11-14 (ICON-D2-Ära)
        # Wetter: wx_t0 (hour_utc = 2023-12-31 23:00) fehlt im Cache → NULL,
        # Bias ebenfalls NULL; wx_t1 (00:00 UTC) = 10.0 → Bias = 11.0 (DJF +1.0)
        assert d["wx_t0_t"][i0] is None
        assert d["wx_t0_t_bias"][i0] is None
        assert d["wx_t1_t"][i0] == 10.0
        assert d["wx_t1_t_bias"][i0] == pytest.approx(11.0, abs=1e-9)
        # 2. Stunde: wx_t0 vorhanden (hour_utc = 2024-01-01 00:00)
        i1 = 1
        assert d["wx_t0_t"][i1] == 10.0
        assert d["wx_t0_t_bias"][i1] == pytest.approx(11.0, abs=1e-9)
        # Lags
        assert d["ext_lag1"][1] == d["ext"][0]
        assert d["ext_lag1"][0] is None
        assert d["ext_lag24"][i0] is None
        # Source-Wechsel bei Stunde 24 (cmi-Quelle)
        assert d["source"][24] == "cmi_log_pq"

    def test_runtime_window_and_since_cap(self, tmp_path, con):
        """24-h-Summe ist rollierend; since_h reicht bis 48, danach gilt der Sentinel 49."""
        start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
        hours = [start + timedelta(hours=i) for i in range(50)]
        tables = [
            make_source_rows(h, 360, burner_vals=[1.0 if i == 0 else 0.0] * 360)
            for i, h in enumerate(hours)
        ]
        half = len(tables) // 2
        winsol, cmi = tmp_path / "winsol", tmp_path / "cmi"
        for batch, root in ((tables[:half], winsol), (tables[half:], cmi)):
            path = root / "2024" / "01" / "2024-01.parquet"
            path.parent.mkdir(parents=True)
            pq.write_table(pa.concat_tables(batch), path)
        cache = tmp_path / "wx"
        make_wx_cache(cache, [], [(2024, 1)])
        cfg = config.BuildConfig(
            features_dir=tmp_path / "features", wx_cache_dir=cache, winsol_pq=winsol,
            cmi_log_pq=cmi, bias_profile=tmp_path / "bias.json", first_hour=start,
        )
        d = assemble_features(con, cfg, None, "git", "ts").to_pydict()
        assert d["oel_burner_on_sum3h"][:4] == [1.0, 1.0, 1.0, 0.0]
        assert d["oel_burner_on_sum24h"][23] == 1.0
        assert d["oel_burner_on_sum24h"][24] == 0.0
        assert d["oel_burner_on_since_h"][0] == 0
        assert d["oel_burner_on_since_h"][48] == 48
        assert d["oel_burner_on_since_h"][49] == 49

    def test_gap_hours_in_grid(self, tmp_path):
        """Fehlende Stunde → gap/gap_full=1, State-NULL, Lags überspringen Lücke."""
        root = tmp_path / "src"
        start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
        # 04:00 fehlt komplett; 07:00 nur 30 Rows (gap_full mit Rows)
        w_rows = pa.concat_tables(
            [make_source_rows(start + timedelta(hours=i), 30 if i == 7 else 360)
             for i in range(20) if i != 4]
        )
        p = root / "winsol" / "2024" / "01" / "2024-01.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(w_rows, p)
        cache = tmp_path / "wx"
        make_wx_cache(cache, [], [(2024, 1)])
        cfg = config.BuildConfig(
            features_dir=tmp_path / "features", wx_cache_dir=cache,
            winsol_pq=root / "winsol", cmi_log_pq=root / "cmi",
            fetch_end="2024-01-31", first_hour=start,
        )
        con = duckdb.connect()
        tbl = assemble_features(con, cfg, None, "git", "ts")
        d = tbl.to_pydict()
        assert tbl.num_rows == 20
        i4 = 4  # 04:00 fehlt komplett
        assert d["gap"][i4] == 1 and d["gap_full"][i4] == 1
        assert d["n_rows"][i4] == 0
        assert d["ext"][i4] is None
        assert d["source"][i4] == "none"
        # Lag-Semantik über die Lücke: lag1 der 05:00-Std = Wert der 04:00 (NULL),
        # lag2 = Wert der 03:00
        assert d["ext_lag1"][5] is None
        assert d["ext_lag2"][5] == d["ext"][3]
        # m1.1: neue Spalten folgen dem Gap-Verhalten der übrigen State-Spalten
        assert d["ext_last"][i4] is None and d["ras_age_kell"][i4] is None
        assert d["heat_min_kell"][i4] is None and d["ras_kell_ist_last"][i4] is None
        i7 = 7
        assert d["gap_full"][i7] == 1 and d["n_rows"][i7] == 30
        assert d["ext"][i7] is not None and d["ext_last"][i7] == d["ext"][i7]
        assert d["ras_age_kell"][i7] == 29.0  # MAX 0…29
        for i in range(tbl.num_rows):
            for f in config.LAST_VALUE_FEATURES:
                assert (d[f][i] is None) == (d[f"{f}_last"][i] is None), (i, f)
            assert (d["ras_kell_ist"][i] is None) == (d["ras_age_kell"][i] is None), i
        # m1.2: hzrkl_* folgen ebenfalls dem Gap-Verhalten (+ Lags über die Lücke)
        for z in config.HZRKL_ZONES:
            for kind in ("on", "last", "pos", "pos_last"):
                assert d[f"hzrkl_{z}_{kind}"][i4] is None, (z, kind)
                assert d[f"hzrkl_{z}_{kind}"][i7] is not None, (z, kind)
        assert d["hzrkl_kell_on"][i7] == pytest.approx(8 / 30)  # i % 4 == 0 bei 30 Rows
        assert d["hzrkl_kell_on_lag1"][5] is None
        assert d["hzrkl_kell_on_lag2"][5] == d["hzrkl_kell_on"][3]
        assert d["hzrkl_dg_pos_lag1"][5] is None
        con.close()


class TestStoreFeatures:
    def test_write_months_and_meta(self, small_env):
        """store_features: Monats-Datei + key_value_Metadaten + _meta.json."""
        cfg = small_env
        con = duckdb.connect()
        bias_profile = {s: {"offset_k": 0.0, "drift_k_per_day": 0.0}
                        for s in config.BIAS_SEASONS}
        parts = store_features(con, cfg, bias_profile, "testgit", "2026-01-01T00:00:00")
        assert len(parts) == 1
        p = parts[0]
        assert p == cfg.features_dir / "2024" / "01" / "2024-01.parquet"
        md = pq.read_metadata(p).metadata
        assert md[b"data_version"] == b"m1.3"
        assert md[b"git_tag"] == b"testgit"
        assert md[b"rows"] == str(48).encode()
        assert pq.ParquetFile(p).metadata.num_rows == 48
        meta_p = store_meta(cfg, {"sources": {}}, bias_profile, {"era5": {}},
                            "testgit", "2026-01-01T00:00:00")
        assert meta_p == cfg.features_dir / "_meta.json"
        data = json.loads(meta_p.read_text())
        assert data["data_version"] == "m1.3"
        assert data["git_tag"] == "testgit"
        con.close()


class TestReport:
    def test_coverage_and_gaps(self, small_env):
        """report.coverage + feature_gaps auf dem synthetischen Store."""
        cfg = small_env
        con = duckdb.connect()
        bias_profile = {s: {"offset_k": 0.0, "drift_k_per_day": 0.0}
                        for s in config.BIAS_SEASONS}
        features.store_features(con, cfg, bias_profile, "git", "ts")
        cov = report.coverage(con, cfg)
        assert "winsol_pq" in cov["sources"]
        assert "cmi_log_pq" in cov["sources"]
        assert cov["sources"]["winsol_pq"]["n_rows"] == 24 * 360
        assert cov["sources"]["cmi_log_pq"]["n_rows"] == 24 * 360
        assert cov["feature_store"]["files"] == 1
        assert cov["feature_store"]["rows"] == 48
        assert cov["feature_store"]["data_version"] == "m1.3"
        gaps = report.feature_gaps(con, cfg)
        assert gaps == []  # keine Lücken in der kleinen Umgebung
        con.close()

    def test_spot_checks(self, small_env):
        """report.spot_checks: Feature-Store vs Roh-Parquet (deterministisch)."""
        cfg = small_env
        con = duckdb.connect()
        bias_profile = {s: {"offset_k": 0.0, "drift_k_per_day": 0.0}
                        for s in config.BIAS_SEASONS}
        features.store_features(con, cfg, bias_profile, "git", "ts")
        checks = report.spot_checks(con, cfg, n=2, seed=42)
        assert len(checks) == 2
        for c in checks:
            for key in ("ext", "hz_vl", "offset_kell", "oel_burner_on"):
                assert c[key]["ok"], f"{c['ts']} {key} weicht ab"
        con.close()


class TestBuildCli:
    def test_git_tag_and_now(self):
        tag = build._git_tag()
        assert isinstance(tag, str) and len(tag) > 0
        now = build._now_iso()
        assert now.endswith("Z") or "+" in now

    def test_main_requires_command(self):
        with pytest.raises(SystemExit):
            build.main([])

    def test_main_weather_with_mock(self, monkeypatch, tmp_path):
        """``m1 weather`` mit gecompletem Cache → 0 neue Requests, rc=0."""
        from analysis.model.m1 import backfill
        cache = tmp_path / "wx"
        make_wx_cache(cache,
                      [(y, m) for y, m in backfill.era5_month_range()],
                      [(y, m) for y, m in backfill.hfa_month_range("2026-10-31")])
        cfg = config.BuildConfig(wx_cache_dir=cache, fetch_end="2026-10-31")
        captured = {}

        def fake_weather(args):
            captured["args"] = args
            stats = backfill.run_backfill(cfg=cfg, sleep_sec=0.0,
                                          get_json=lambda u: (_ for _ in ()).throw(AssertionError("no http")),
                                          log=lambda *a: None)
            return 0 if not stats.failures else 2

        monkeypatch.setattr(build, "cmd_weather", fake_weather)
        rc = build.main(["weather", "--quiet"])
        assert rc == 0
        assert captured["args"].quiet is True


# --- m1.1 (M2b-P2): RAS-Maske im Feature-Store, neue Spalten, m1_1_checks ----------

def _ras_derived(cols: list[str]) -> list[str]:
    """RAS-abgeleitete Store-Spalten (inkl. Lags/_last), offset_known separat."""
    return [c for c in cols
            if c.startswith(("ras_", "ras2_", "offset_", "heat_min_")) and c != "offset_known"]


@pytest.fixture
def mask_env(tmp_path):
    """12 h ab 2021-10-20 12:00 (5 h vor RAS-Einbau 17:00, 7 h danach), base = 20 + i."""
    start = datetime(2021, 10, 20, 12, 0, tzinfo=BERLIN)
    rows = pa.concat_tables(
        [make_source_rows(start + timedelta(hours=i), 360, base=20.0 + i) for i in range(12)]
    )
    p = tmp_path / "src" / "winsol" / "2021" / "10" / "2021-10.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows, p)
    cache = tmp_path / "wx"
    make_wx_cache(cache, [(2021, 10)], [])
    return config.BuildConfig(
        features_dir=tmp_path / "features", wx_cache_dir=cache,
        winsol_pq=tmp_path / "src" / "winsol", cmi_log_pq=tmp_path / "src" / "cmi",
        fetch_end="2021-10-31", first_hour=start,
    )


class TestM11FeatureStore:
    def test_new_columns_present_and_unique(self, small_env, con):
        tbl = assemble_features(con, small_env, None, "git", "ts")
        cols = tbl.column_names
        assert len(cols) == len(set(cols)), "doppelte Spaltennamen"
        for c in ("ras_age_kell", "ras_age_eg", "ras_age_dg",
                  "heat_min_kell", "heat_min_eg", "heat_min_dg"):
            assert c in cols, c
        for f in config.LAST_VALUE_FEATURES:
            assert f"{f}_last" in cols, f
        # LAG_COLUMNS unverändert: keine Lags auf ras_age_* / {feat}_last
        assert not any(c.startswith("ras_age_") and "_lag" in c for c in cols)
        assert not any(c.endswith("_last_lag1") for c in cols)
        d = tbl.to_pydict()
        assert d["ras_age_kell"][0] == 359.0       # MAX 0…359
        assert d["ext_last"][0] == 20.0
        assert d["ras_kell_perc_last"][0] == 21.0

    def test_ras_mask_before_install(self, mask_env, con):
        """Vor 2021-10-20 17:00: alle RAS-abgeleiteten Spalten (inkl. Lags/_last) NULL,
        offset_known = 0; ab 17:00 Werte; Lags folgen der Maske; Nicht-RAS unberührt."""
        tbl = assemble_features(con, mask_env, None, "git", "ts")
        d = tbl.to_pydict()
        assert tbl.num_rows == 12
        install = datetime.fromisoformat(config.RAS_INSTALL_TS)
        i17 = d["ts"].index(install)
        assert i17 == 5
        ras_cols = _ras_derived(tbl.column_names)
        assert len(ras_cols) > 60
        for i in range(i17):
            assert [c for c in ras_cols if d[c][i] is not None] == [], d["ts"][i]
            assert d["offset_known"][i] == 0
            assert d["ext"][i] == 20.0 + i and d["ext_last"][i] == 20.0 + i
            assert d["buff_top_last"][i] is not None and d["solar_coll_last"][i] is not None
        for i in range(i17, 12):
            assert d["offset_known"][i] == 1
            assert d["ras_kell_ist"][i] == 20.0 + i
            assert d["ras_kell_ist_last"][i] == 20.0 + i
            assert d["ras_dg_perc_last"][i] == 21.0 + i
            assert d["offset_kell"][i] == 1.0
            assert d["ras2_kell_ist"][i] == 20.0 + i
            assert d["ras_divergence_kell"][i] == 0.0
            assert d["ras_age_kell"][i] == 359.0
            assert d["heat_min_kell"][i] == 359.0
        # Lags: lag1 der ersten Einbau-Stunde zeigt auf 16:00 (maskiert) → NULL
        assert d["ras_kell_ist_lag1"][i17] is None
        assert d["ras_kell_ist_lag1"][i17 + 1] == d["ras_kell_ist"][i17]
        assert d["ras_kell_ist_lag6"][i17 + 5] is None
        assert d["ras_kell_ist_lag6"][i17 + 6] == d["ras_kell_ist"][i17]
        assert d["heat_min_kell_lag1"][i17] is None
        assert d["ext_lag1"][i17] == d["ext"][i17 - 1]

    def test_m1_1_checks_and_spot_checks(self, mask_env):
        con = duckdb.connect()
        store_features(con, mask_env, None, "git", "ts")
        chk = report.m1_1_checks(con, mask_env)
        assert chk["rows"] == 12
        assert chk["data_versions"] == ["m1.3"]
        assert chk["before"]["hours"] == 5
        assert chk["before"]["hours_ras_nonnull"] == 0
        assert chk["before"]["hours_offset_known"] == 0
        assert set(chk["ist_below_5"]) >= {"ras_kell_ist", "ras_kell_ist_last", "ras2_dg_ist"}
        assert all(v == 0 for v in chk["ist_below_5"].values())
        for zone in ("kell", "eg", "dg"):
            a = chk["ras_age"][zone]
            assert a["n"] == 7
            assert a["p50"] == a["p999"] == a["max"] == 359.0
            assert a["share_stale"] == 1.0
        assert chk["last_corr"]["ras_kell_ist"] == pytest.approx(1.0)
        assert chk["last_corr"]["ext"] == pytest.approx(1.0)
        # Spot-Checks über alle 12 h: offset_kell vor Einbau NULL == maskierter Roh-Median
        checks = report.spot_checks(con, mask_env, n=12, seed=1)
        assert len(checks) == 12
        for c in checks:
            for key in ("ext", "hz_vl", "offset_kell", "oel_burner_on"):
                assert c[key]["ok"], f"{c['ts']} {key}"
        con.close()

    def test_m1_1_checks_missing_or_legacy_store(self, tmp_path):
        con = duckdb.connect()
        cfg = config.BuildConfig(features_dir=tmp_path / "keine-features")
        assert report.m1_1_checks(con, cfg) == {}
        legacy = tmp_path / "legacy"
        p = legacy / "2021" / "01" / "2021-01.parquet"
        p.parent.mkdir(parents=True)
        pq.write_table(pa.table({"ts": pa.array([datetime(2021, 1, 1)], type=pa.timestamp("us")),
                                 "heat_min_kell": pa.array([6.0])}), p)
        assert report.m1_1_checks(con, config.BuildConfig(features_dir=legacy)) == {}
        con.close()


# --- m1.2: TA-Sentinel-Maske im Store, hzrkl_*, m1_2_checks ------------------------

@pytest.fixture
def sentinel_env(tmp_path):
    """6 h ab 2024-01-01 00:00 (base = 20 + i). Stunde 2: flue (dev0 a10) zur Hälfte
    3276.7; Stunde 3: solar_coll (dev2 a16) komplett 3276.7, ras_kell_ist 10× -999.9."""
    start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
    tables = []
    for i in range(6):
        t = make_source_rows(start + timedelta(hours=i), 360, base=20.0 + i)
        if i == 2:
            flue = [20.0 + i if j % 2 == 0 else 3276.7 for j in range(360)]
            t = t.set_column(t.schema.get_field_index("dev0_analog_10"), "dev0_analog_10",
                             pa.array(flue, type=pa.float64()))
        if i == 3:
            t = t.set_column(t.schema.get_field_index("dev2_analog_16"), "dev2_analog_16",
                             pa.array([3276.7] * 360, type=pa.float64()))
            ist = [-999.9 if j >= 350 else 20.0 + i for j in range(360)]
            t = t.set_column(t.schema.get_field_index("dev1_analog_02"), "dev1_analog_02",
                             pa.array(ist, type=pa.float64()))
        tables.append(t)
    p = tmp_path / "src" / "winsol" / "2024" / "01" / "2024-01.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.concat_tables(tables), p)
    cache = tmp_path / "wx"
    make_wx_cache(cache, [], [(2024, 1)])
    return config.BuildConfig(
        features_dir=tmp_path / "features", wx_cache_dir=cache,
        winsol_pq=tmp_path / "src" / "winsol", cmi_log_pq=tmp_path / "src" / "cmi",
        fetch_end="2024-01-31", first_hour=start,
    )


class TestM12FeatureStore:
    def test_hzrkl_columns_values_and_lags(self, small_env, con):
        tbl = assemble_features(con, small_env, None, "git", "ts")
        cols = tbl.column_names
        assert len(cols) == len(set(cols))
        for z in config.HZRKL_ZONES:
            for kind in ("on", "last", "pos", "pos_last"):
                assert f"hzrkl_{z}_{kind}" in cols
            for k in config.LAG_HOURS:
                assert f"hzrkl_{z}_on_lag{k}" in cols and f"hzrkl_{z}_pos_lag{k}" in cols
                assert f"hzrkl_{z}_rule_lag{k}" in cols
            assert not any(c.startswith(f"hzrkl_{z}_last_lag") for c in cols)
        assert len(LAG_COLUMNS) == 34
        d = tbl.to_pydict()
        for i in (0, 30):
            assert d["hzrkl_kell_on"][i] == 0.25 and d["hzrkl_kell_last"][i] == 0.0
            assert d["hzrkl_eg_on"][i] == 0.25 and d["hzrkl_dg_on"][i] == 0.25
            assert d["hzrkl_kell_pos"][i] == 20.0 and d["hzrkl_kell_pos_last"][i] == 20.0
            assert d["hzrkl_dg_pos"][i] == 22.0 and d["hzrkl_dg_pos_last"][i] == 22.0  # a14
            assert d["hzrkl_eg_pos"][i] == 23.0 and d["hzrkl_eg_pos_last"][i] == 23.0  # a15
        assert d["hzrkl_kell_on_lag1"][0] is None
        assert d["hzrkl_kell_on_lag1"][1] == d["hzrkl_kell_on"][0]
        assert d["hzrkl_eg_pos_lag24"][24] == d["hzrkl_eg_pos"][0]
        assert d["hzrkl_eg_pos_lag24"][23] is None

    def test_sentinels_masked_in_store(self, sentinel_env, con):
        tbl = assemble_features(con, sentinel_env, None, "git", "ts")
        d = tbl.to_pydict()
        assert tbl.num_rows == 6 and all(n == 360 for n in d["n_rows"])
        assert d["flue"][2] == 220.0 and d["flue_last"][2] == 220.0  # ×10; letzte Row = Sentinel
        assert d["solar_coll"][3] is None and d["solar_coll_last"][3] is None
        assert d["solar_coll_lag1"][4] is None
        assert d["solar_coll_lag2"][4] == d["solar_coll"][2] == 22.0
        assert d["ras_kell_ist"][3] == 23.0 and d["ras_kell_ist_last"][3] == 23.0
        assert d["offset_kell"][3] == 1.0
        assert max(v for v in d["flue"] if v is not None) <= config.FLUE_MAX_C

    def test_m1_2_checks(self, sentinel_env, monkeypatch):
        con = duckdb.connect()
        store_features(con, sentinel_env, None, "git", "ts")
        assert report.sentinel_counts(con, sentinel_env) == {
            "dev0_analog_10": {"hi": 180, "lo": 0},
            "dev1_analog_02": {"hi": 0, "lo": 10},
            "dev2_analog_16": {"hi": 360, "lo": 0},
        }
        monkeypatch.setattr(report, "HZRKL_SEASON", ("2024-01-01 02:00:00", "2024-01-01 04:00:00"))
        chk = report.m1_2_checks(con, sentinel_env)
        assert chk["rows"] == 6
        assert chk["data_versions"] == ["m1.3"]
        f0 = next(sentinel_env.features_dir.glob("*/*/*.parquet"))
        assert chk["cols"] == len(pq.read_schema(f0))
        assert chk["max"]["flue"] == 250.0 and chk["max"]["solar_coll"] == 25.0
        assert chk["max"]["flue_last"] == 250.0
        assert chk["max_last_overall"] == 250.0  # flue_last ist jetzt in °C skaliert
        assert chk["flue_hours_below_minus50"] == chk["flue_last_hours_below_minus50"] == 0
        h = chk["hzrkl"]["dg"]
        assert h["season_corr_on_perc"] is None     # on konstant
        assert h["first_on"] == "2024-01-01 00:00:00" and h["first_pos"] == "2024-01-01 00:00:00"
        assert h["share_on"] == h["share_pos"] == 1.0
        assert h["share_on_since_first"] == h["share_pos_since_first"] == 1.0
        assert h["season_mean_on"] == 0.25
        assert h["season_mean_pos"] == pytest.approx(24.5)  # Stunden 2, 3: base + 2
        assert h["corr_on_perc"] is None            # on konstant → corr undefiniert
        assert h["corr_pos_perc"] == pytest.approx(1.0)
        report_cfg_missing = config.BuildConfig(features_dir=sentinel_env.features_dir.parent / "x")
        assert report.m1_2_checks(con, report_cfg_missing) == {}
        con.close()

    def test_m1_2_checks_legacy_store(self, tmp_path):
        legacy = tmp_path / "legacy"
        p = legacy / "2021" / "01" / "2021-01.parquet"
        p.parent.mkdir(parents=True)
        pq.write_table(pa.table({"ts": pa.array([datetime(2021, 1, 1)], type=pa.timestamp("us")),
                                 "flue": pa.array([600.0])}), p)
        con = duckdb.connect()
        assert report.m1_2_checks(con, config.BuildConfig(features_dir=legacy)) == {}
        con.close()
