"""Tests: Pipeline-CLI (``build.py``) + ``report.build_report`` (synthetisch).

Kein echtes Netzwerk, keine Produktions-Pfade: alle Kommandos gegen
tmp-Features/Cache-Umgebung (``build._cfg`` + ``build.config.con`` +
report-Modul-Attribute gepatcht).
"""
import argparse
import json
from datetime import datetime

import duckdb
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import backfill, bias, build, config, features
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.tests.test_integration import (
    make_small_sources,
    make_wx_cache,
)


@pytest.fixture
def env(tmp_path):
    """Kleine Produktions-ähnliche Umgebung (48 h, 1 Monat, Cache gefüllt)."""
    start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
    winsol, cmi = make_small_sources(tmp_path / "src", n_hours=48, start=start)
    cache = tmp_path / "wx"
    make_wx_cache(cache, [], [(2024, 1)])
    return config.BuildConfig(
        features_dir=tmp_path / "features",
        wx_cache_dir=cache,
        winsol_pq=winsol,
        cmi_log_pq=cmi,
        bias_profile=tmp_path / "bias_profile.json",
        fetch_end="2024-01-31",
        first_hour=start,
    )


@pytest.fixture
def patched_build(env, monkeypatch):
    """build._cfg → env, build.config.con → frische In-Memory-Verbindung."""
    monkeypatch.setattr(build, "_cfg", lambda args: env)
    monkeypatch.setattr(build.config, "con", lambda: duckdb.connect())
    return env


@pytest.fixture
def ok_weather(monkeypatch):
    """run_backfill → sauberer FetchStats (kein echtes HTTP in CLI-Tests)."""
    monkeypatch.setattr(
        build.backfill, "run_backfill",
        lambda **kw: backfill.FetchStats(requests=0, skipped_cached=43),
    )


@pytest.fixture
def patched_report(env, monkeypatch):
    """report-Modul-Attribute → tmp-Pfade."""
    from analysis.model.m1 import report as rep
    monkeypatch.setattr(rep, "REPORT_PATH", env.features_dir / "REPORT.md")
    monkeypatch.setattr(rep, "FEATURES_DIR", env.features_dir)
    monkeypatch.setattr(rep, "WX_CACHE_DIR", env.wx_cache_dir)
    monkeypatch.setattr(rep, "BIAS_PROFILE_PATH", env.bias_profile)
    monkeypatch.setattr(rep, "WINSOL_PQ", env.winsol_pq)
    monkeypatch.setattr(rep, "CMI_LOG_PQ", env.cmi_log_pq)
    return env


def _ns(**kw):
    base = dict(fetch_end="2024-01-31", force=False, quiet=True)
    base.update(kw)
    return argparse.Namespace(**base)


class TestCliCommands:
    def test_cmd_weather_cached_ok(self, env, patched_build, ok_weather):
        rc = build.cmd_weather(_ns())
        assert rc == 0

    def test_cmd_weather_failures_rc2(self, env, monkeypatch):
        fake = backfill.FetchStats(failures=1, missing_months=["2024-01 (x: boom)"])
        monkeypatch.setattr(build.backfill, "run_backfill",
                            lambda **kw: fake)
        assert build.cmd_weather(_ns()) == 2

    def test_cmd_bias_writes_profile(self, patched_build, env):
        rc = build.cmd_bias(_ns())
        assert rc == 0
        assert env.bias_profile.exists()
        data = json.loads(env.bias_profile.read_text())
        assert "profile" in data and "DJF" in data["profile"]

    def test_cmd_features_writes_store(self, patched_build, env):
        con0 = duckdb.connect()
        bias.prepare_con(con0, env)
        res = bias.compute_bias(con0, env)
        bias.save_profile(res, env.bias_profile)
        con0.close()
        rc = build.cmd_features(_ns())
        assert rc == 0
        files = list(env.features_dir.glob("*/*/*.parquet"))
        assert len(files) == 1
        assert pq.ParquetFile(files[0]).metadata.num_rows == 48
        meta = json.loads((env.features_dir / "_meta.json").read_text())
        assert meta["data_version"] == "m1.3"
        assert meta["coverage"]["feature_store"]["rows"] == 48
        assert meta["wx_cache"]["hfa"]["valid"] == 1

    def test_cmd_report_writes_md(self, patched_build, patched_report, env):
        con0 = duckdb.connect()
        bias.prepare_con(con0, env)
        res = bias.compute_bias(con0, env)
        bias.save_profile(res, env.bias_profile)
        con0.close()
        con1 = duckdb.connect()
        features.store_features(con1, env, res.as_profile(), "git", "ts")
        con1.close()
        rc = build.cmd_report(_ns())
        assert rc == 0
        out = env.features_dir / "REPORT.md"
        assert out.exists()
        assert "M1 — REPORT" in out.read_text()

    def test_cmd_all(self, patched_build, patched_report, env, ok_weather):
        rc = build.cmd_all(_ns())
        assert rc == 0
        assert env.bias_profile.exists()
        assert list(env.features_dir.glob("*/*/*.parquet"))
        assert (env.features_dir / "REPORT.md").exists()

    def test_cmd_all_aborts_on_weather_failure(self, patched_build, monkeypatch):
        fake = backfill.FetchStats(failures=1, missing_months=["x"])
        monkeypatch.setattr(build.backfill, "run_backfill", lambda **kw: fake)
        assert build.cmd_all(_ns()) == 2


class TestMain:
    def test_main_dispatch_weather(self, patched_build, ok_weather):
        assert build.main(["weather", "--quiet"]) == 0

    def test_main_dispatch_features(self, patched_build):
        con0 = duckdb.connect()
        bias.prepare_con(con0, patched_build)
        res = bias.compute_bias(con0, patched_build)
        bias.save_profile(res, patched_build.bias_profile)
        con0.close()
        assert build.main(["features", "--quiet"]) == 0

    def test_main_unknown_command_exits(self):
        with pytest.raises(SystemExit):
            build.main(["bogus"])

    def test_main_no_command_exits(self):
        with pytest.raises(SystemExit):
            build.main([])

    def test_main_fetch_end_option(self, patched_build, ok_weather):
        assert build.main(["weather", "--fetch-end", "2024-01-31", "--quiet"]) == 0


class TestReportBuilder:
    def test_build_report_full(self, patched_build, patched_report, env):
        from analysis.model.m1 import report as rep
        con0 = duckdb.connect()
        bias.prepare_con(con0, env)
        res = bias.compute_bias(con0, env)
        bias.save_profile(res, env.bias_profile)
        con0.close()
        con1 = duckdb.connect()
        features.store_features(con1, env, res.as_profile(), "git", "ts")
        con1.close()
        p = rep.build_report()
        assert p == env.features_dir / "REPORT.md"
        text = p.read_text()
        for section in ("## 1. Daten-Coverage", "## 2. Bias-Report",
                        "## 3. Wetter-Backfill", "## 4. Feature-Store",
                        "## 5. Spot-Checks", "## 6. DoD-Checkliste",
                        "## 7. m1.1", "## 8. m1.2", "## 9. m1.3"):
            assert section in text, f"Sektion fehlt: {section}"
        assert "winsol_pq" in text and "cmi_log_pq" in text
        assert "data_version `m1.3`" in text
        assert "| kell | 48 |" in text  # ras_age-Tabelle (48 h nach RAS-Einbau)
        assert "Keine Sentinel-Samples in den Roh-Quellen" in text
        assert "| kell | 2024-01-01 00:00:00 |" in text  # hzrkl-Tabelle (m1.2)

    def test_build_report_missing_store(self, patched_report, monkeypatch):
        from analysis.model.m1 import report as rep
        monkeypatch.setattr(rep, "FEATURES_DIR",
                            patched_report.features_dir.parent / "keine-features")
        p = rep.build_report()
        text = p.read_text()
        assert "fehlt" in text
        assert "DoD-Checkliste" in text
