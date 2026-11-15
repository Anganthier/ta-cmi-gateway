"""CLI (``python -m analysis.model.l2 recommend``): Argument-Parsing + DE-Output.

``recommend`` wird ge-mockt (kein echtes L1/LIVE-Forecast im CLI-Test); nur
die Orchestrierung (Pfad-Bindung, JSON- vs. DE-Zusammenfassung, ``--no-save``)
wird geprüft.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from analysis.model.l2 import __main__ as cli
from analysis.model.l2 import config
from analysis.model.l2.recommend import Recommendation


def _canned_rec(ts: str = "2026-11-01 11:00") -> Recommendation:
    """Minimal gültige Empfehlung (stale, keine Rollouts)."""
    state = _canned_state()
    return Recommendation(
        ts=ts,
        model_version=config.model_version(),
        horizon_h=12,
        state=state,
        wx=None,
        stale=True,
        stale_reasons=["LIVE-Forecast nicht erreichbar → Fallback auf Store-Wetter (wx_cache/Ist)"],
        empfehlungen=[
            {
                "parameter": f"RAS-Offset {z}",
                "zone": z,
                "aktuell": "+0.0 K",
                "vorgeschlagen": "+0.0 K (beibehalten)",
                "wirkung_prognose": "keine Änderung",
                "begruendung_de": "Stale: beibehalten.",
                "konfidenz": 0.03,
                "executability": config.EXECUTABILITY_PENDING,
                "cmi_hint": config.CMI_HINTS[f"offset_{z}"],
                "tier": "A",
                "changed": False,
            }
            for z in config.ZONES
        ],
        kosten={"basis": {}, "beste": {}, "kandidaten": [], "gewichte": {}},
        guardrail_hits=[],
        konfidenz=0.03,
        n_candidates=0,
        n_before_prune=0,
        candidate_list=[],
    )


def _canned_state() -> Any:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from analysis.model.l2.recommend import State

    return State(
        ts_str="2026-11-01 11:00",
        ts=datetime(2026, 11, 1, 11, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        rows={"offset_kell": 0.0, "offset_eg": 0.0, "offset_dg": 0.0},
        state_ts_str="2026-11-01 11:00:00",
        data_version="m1.3",
        store_last_ts="2026-11-01 15:00:00",
        metric_gap_h=7.0,
        requested_missing=True,
        gap_full=False,
    )


def test_cli_recommend_json(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                           tmp_path: Any) -> None:
    """``--json`` → JSON nach Stdout (parsebar), exit 0."""
    monkeypatch.setattr(cli, "recommend", lambda *a, **k: _canned_rec())
    rc = cli.main(["recommend", "--ts", "2026-11-01 11:00", "--json", "--no-save"])
    assert rc == 0
    out = capsys.readouterr().out
    j = json.loads(out)
    assert j["ts"] == "2026-11-01 11:00"
    assert j["stale"] is True


def test_cli_recommend_de_summary(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Any) -> None:
    """Default → DE-Zusammenfassung (keines der JSON-Rohdaten-Blöcke)."""
    monkeypatch.setattr(cli, "recommend", lambda *a, **k: _canned_rec())
    rc = cli.main(["recommend", "--ts", "2026-11-01 11:00", "--no-save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "L2 v0.2" in out
    assert "stale: True" in out
    assert "cmi-brain-v003" in out


def test_cli_default_ts_now(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Ohne ``--ts`` = jetzt (volle Berliner Stunde)."""
    captured: dict[str, Any] = {}

    def fake_recommend(ts: str, **k: Any) -> Recommendation:
        captured["ts"] = ts
        return _canned_rec()

    monkeypatch.setattr(cli, "recommend", fake_recommend)
    rc = cli.main(["recommend", "--no-save"])
    assert rc == 0
    assert "2026-" in captured["ts"] or "2025-" in captured["ts"]
    assert len(captured["ts"]) == len("2026-11-01 11:00")


def test_cli_no_wx_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """``--no-wx`` → Fallback (Store-Wetter, stale), kein Netzwerk."""
    captured: dict[str, Any] = {}

    def fake_recommend(ts: str, **k: Any) -> Recommendation:
        captured["wx"] = k.get("wx")
        return _canned_rec()

    monkeypatch.setattr(cli, "recommend", fake_recommend)
    rc = cli.main(["recommend", "--no-wx", "--no-save"])
    assert rc == 0
    assert captured["wx"] is None


def test_cli_estimate_lags(tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    """v0.5 Ticket D: ``estimate-lags``-Subkommando (DE-Output, exit 0).

    ``estimate_lags`` wird ge-mockt (keine echten Rohdaten/keine Schätzung
    im CLI-Test); nur die Orchestrierung (Subkommando-Pfad, Zonen-Output,
    Default-Artefakt-Pfad) wird geprüft."""
    from analysis.model.l2 import rm_diagnostics as rm_diag

    art = {
        "generated_at": "2026-11-07 09:00:00",
        "window_days": 30,
        "method": "cross-correlation …",
        "n_samples_total": 1000,
        "zones": {
            "kell": {"lag_s": 30, "confidence": 0.4, "source": "default", "n": 0, "corr": None},
            "eg": {"lag_s": 300, "confidence": 0.7, "source": "estimate", "n": 60000, "corr": 0.9},
            "dg": {"lag_s": 460, "confidence": 0.6, "source": "estimate", "n": 36000, "corr": 0.8},
            "central": {"lag_s": 270, "confidence": 0.7, "source": "estimate", "n": 70000, "corr": 0.9},
        },
    }

    calls: dict[str, Any] = {}

    def fake_estimate_lags(data_dir=None, window_days=30, now=None, out_path=None):
        calls["window_days"] = window_days
        return art

    monkeypatch.setattr(rm_diag, "estimate_lags", fake_estimate_lags)
    rc = cli.main(["estimate-lags", "--window-days", "30"])
    assert rc == 0
    assert calls["window_days"] == 30
    out = capsys.readouterr().out
    assert "L2 v0.5 · Lag-Schätzung" in out
    assert "eg     : lag 300 s" in out
    assert "kell   : lag 30 s" in out
    assert "central: lag 270 s" in out
    assert "out:" in out


def test_cli_calibrate_vl(tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    """v0.5 Ticket C: ``calibrate-vl``-Subkommando (DE-Output, exit 0).

    ``calibrate_vl`` wird ge-mockt (kein echter Feature-Store im CLI-Test);
    nur die Orchestrierung (Subkommando-Pfad, Band-Output, Rebuild-Hinweis,
    Default-Artefakt-Pfad) wird geprüft."""
    from analysis.model.l2 import vl_calibration as vl_cal

    art = {
        "generated_at": "2026-11-07 09:00:00",
        "window_days": 365,
        "window_since": "2025-11-07 00:00:00",
        "method": "VL-Ø je Außen-T-Band (3 K) × Heizstunden-Only …",
        "n_heizstunden": 1234,
        "n_oel_brenner_stunden": 40,
        "min_n_per_band": 50,
        "band_k": 3,
        "bands": {
            "6": {"band_lo": 6, "band_hi": 9, "ext_mean": 7.1, "vl_mean": 50.5,
                  "curve_vl_soll": 50.9, "dev_k": -0.4, "n": 600, "usable": True},
            "9": {"band_lo": 9, "band_hi": 12, "ext_mean": 10.0, "vl_mean": 47.0,
                  "curve_vl_soll": 48.0, "dev_k": -1.0, "n": 20, "usable": False},
        },
    }

    calls: dict[str, Any] = {}

    def fake_calibrate_vl(features_dir=None, window_days=None, now=None, out_path=None):
        calls["window_days"] = window_days
        calls["features_dir"] = features_dir
        return art

    monkeypatch.setattr(vl_cal, "calibrate_vl", fake_calibrate_vl)
    rc = cli.main(["calibrate-vl", "--window-days", "30"])
    assert rc == 0
    assert calls["window_days"] == 30
    out = capsys.readouterr().out
    assert "L2 v0.5 · VL-Soll-Kalibrierung" in out
    assert "1234 Heizstunden" in out
    assert "Band    6…   9 °C" in out
    assert "usable" in out
    assert "zu wenig n" in out
    assert "out:" in out
    assert "Rebuild-Befehl" in out
    assert "calibrate-vl" in out
