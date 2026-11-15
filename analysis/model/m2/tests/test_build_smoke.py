"""Tests: Build-Orchestrierung (Smoke) — decide_model_kind, _resp_flat,
_report-Rendering und build() mit gepatchten Trainings-/Metric-Phasen
(KEIN echtes Training, < 1 s; deckt build.py + report.py ab)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from analysis.model.m2 import build, config, report


def _fake_metrics() -> dict[str, object]:
    """Metrik-Dict in Build-Format (alle DoD-Zahlen klein genug → PASS)."""

    def _maes(prefix: float) -> dict[str, float]:
        return {t: float(prefix) for t in config.TARGETS}

    resp = {
        "n_cases": 200,
        "n_runs": 800,
        "deltas_k": list(config.RESP_DELTAS_K),
        "pearson_delta_offset_dH": -0.85,
        "pearson_delta_offset_dD": -0.5,
        "dH_mittel_je_delta": {
            "+1.0K": -0.10, "+2.0K": -0.20, "+3.4K": -0.34, "+5.0K": -0.50
        },
        "dD_mittel_je_delta": {"+3.4K": -0.05},
        "dH_neg_anteil_3.4K": 0.9,
        "dod_corr_ok": True,
        "dod_frac_ok": True,
        "dod_passed": True,
    }
    return {
        "maes_keras_valid_1h": _maes(0.8),
        "maes_b2_valid_1h": _maes(1.2),
        "maes_lgbm_valid_1h": _maes(0.85),
        "maes_keras_valid_4h": _maes(0.9),
        "maes_b2_valid_4h": _maes(1.4),
        "maes_lgbm_valid_4h": _maes(0.95),
        "maes_keras_test_4h": _maes(0.9),
        "maes_lgbm_test_4h": _maes(0.95),
        "maes_b2_test_4h": _maes(1.4),
        "responsivity_keras": dict(resp),
        "responsivity_lgbm": dict(resp),
        "test_2026": {
            "keras": _maes(0.9),
            "lgbm": _maes(0.95),
            "b2": _maes(1.4),
            "worst_day": {
                "date": "2026-02-03",
                "ras_kell_ist": 1.1,
                "ras_eg_ist": 0.9,
                "ras_dg_ist": 1.0,
            },
        },
    }


def test_decide_model_kind_picks_lower_score():
    m = _fake_metrics()
    d = build.decide_model_kind(m)
    # Keras-MAE 0,9 < LGBM 0,95, beide Responsivität OK → Keras gewinnt.
    assert d["model_kind"] == "keras"
    assert d["keras_score"] < d["lightgbm_score"]
    # LGBM-MAE kleiner + Keras-Responsivität scheitert → LightGBM.
    m2 = dict(m)
    m2["maes_lgbm_valid_4h"] = {t: 0.5 for t in config.TARGETS}
    m2["responsivity_keras"] = dict(m["responsivity_keras"], dod_passed=False)
    d2 = build.decide_model_kind(m2)
    assert d2["model_kind"] == "lightgbm"
    assert d2["keras_score"] > d2["lightgbm_score"]


def test_resp_flat_maps_report_keys():
    flat = build._resp_flat(_fake_metrics()["responsivity_keras"])
    assert flat["pearson_dH"] == -0.85
    assert flat["frac_dH_neg_34"] == 0.9
    assert flat["mean_dH_34"] == -0.34
    assert flat["mean_dD_34"] == -0.05
    assert flat["dod_passed"] is True


def _fake_results() -> dict[str, object]:
    return {
        "generated_at": "2026-10-31T12:00:00+00:00",
        "git_rev": "abc1234",
        "tf_version": "2.21.0",
        "keras_version": "3.15.1",
        "lgbm_version": "4.7.0",
        "numpy_version": "2.5.4",
        "stats": {
            "n_rows_total": 1000,
            "n_rows_gap_full": 3,
            "n_drops_gap_current": 2,
            "n_drops_missing_next": 1,
            "n_drops_gap_full_next": 1,
            "n_transitions": 995,
            "n_imputed_cells": 10,
            "split_sizes": {"train": 500, "valid": 300, "test": 195},
        },
        "split_info": {
            "train_start": "2020-08-31T00:00",
            "train_end": "2024-12-31T23:00",
            "valid_start": "2025-01-01T00:00",
            "valid_end": "2025-12-31T23:00",
            "test_start": "2026-01-01T00:00",
            "test_end": "2026-10-31T00:00",
        },
        "n_flag_cols": 430,
        "maes_keras_valid_1h": {t: 0.8 for t in config.TARGETS},
        "maes_b2_valid_1h": {t: 1.2 for t in config.TARGETS},
        "maes_keras_valid_4h": {t: 0.9 for t in config.TARGETS},
        "maes_b2_valid_4h": {t: 1.4 for t in config.TARGETS},
        "maes_lgbm_valid_4h": {t: 0.95 for t in config.TARGETS},
        "responsivity_keras": build._resp_flat(_fake_metrics()["responsivity_keras"]),
        "responsivity_lgbm": build._resp_flat(_fake_metrics()["responsivity_lgbm"]),
        "test_2026": _fake_metrics()["test_2026"],
        "architecture": {
            "n_params": 12345,
            "hidden": [256, 128, 64],
            "head_hidden": 32,
            "dropout": 0.1,
            "n_heads": 31,
            "heads": {t: "linear" for t in config.TARGETS},
        },
        "decision": {"model_kind": "keras", "reason": "Smoke"},
        "responsivity_interpretation": "JA (modellseitig reproduziert).",
        "keras_last_epoch": 20,
        "keras_valid_mae_best": 0.7,
        "keras_valid_history": [1.0, 0.9, 0.7],
        "assumptions": ["Test-Annahme 1", "Test-Annahme 2"],
    }


def test_report_render_smoke(tmp_path):
    """render_report + write_report mit synthetischem Build-Result →
    REPORT.md enthält DoD-Checkliste + Decision (kein File-IO außer Report)."""
    results = _fake_results()
    text = report.render_report(results)
    assert "# M2 — REPORT" in text
    assert "DoD-Checkliste" in text
    assert "✅ DoD erfüllt" in text  # alle DoD grün im Fake-Metrik-Set (Keras gewinnt)
    assert "Keras = model_kind" in text
    assert "model_kind" in text and "`keras`" in text
    p = report.write_report(results, path=tmp_path / "REPORT.md")
    assert p.read_text() == text


def test_val_loss_history_missing_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ckpt")
    assert build._val_loss_history() == []
    (tmp_path / "ckpt").mkdir()
    (tmp_path / "ckpt" / "keras_train_meta.json").write_text(
        json.dumps({"val_loss_history": [1.0, 0.5]})
    )
    assert build._val_loss_history() == [1.0, 0.5]
    (tmp_path / "ckpt" / "keras_train_meta.json").write_text(json.dumps({}))
    assert build._val_loss_history() == []


def test_build_full_smoke(tmp_path, monkeypatch, mini_store):
    """build() End-to-End mit gepatchten Trainings-/Metric-Phasen: kein
    echtes Training, aber kompletter Phasen-Fluss (prepare_data,
    Decision, artifact, report, ready-Flag)."""
    splits, stats, _X_ext, _flags, _scaler = mini_store

    class _FakeModel:
        """Minimal-Keras-Stub (count_params/Architecture-Summary-OK)."""

        weights = []

        def predict(self, X, **kw):
            n = len(np.asarray(X))
            return [np.full(n, 0.1, dtype=np.float32) for _ in config.TARGETS]

        def save(self, path):
            p = __import__("pathlib").Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fake-keras")

    class _FakeEns:
        models = []

        def predict_targets(self, X):
            n = len(np.asarray(X))
            return np.full((n, len(config.TARGETS)), 0.2, dtype=np.float64)

        def save(self, out_dir):
            from pathlib import Path

            d = Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            return {t: (d / f"{t}.txt").write_text("dummy") and (d / f"{t}.txt") for t in config.TARGETS}

    # Trainings-/Metric-Phasen patchen (KEIN echtes Training).
    monkeypatch.setattr(build, "train_keras", lambda *a, **kw: _FakeModel())
    monkeypatch.setattr(build, "train_lgbm", lambda *a, **kw: _FakeEns())
    monkeypatch.setattr(build, "compute_metrics", lambda *a, **kw: _fake_metrics())
    # Transitions aus dem Mini-Store (kein Full-Store-DuckDB-Lauf im Test).
    monkeypatch.setattr(build, "build_transitions", lambda *a, **kw: (splits, stats))
    # Artefakt-/Report-/Checkpoint-Pfade nach tmp_path (echtes Repo intakt).
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ckpt")
    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifact")
    monkeypatch.setattr(config, "REPORT_PATH", tmp_path / "REPORT.md")
    monkeypatch.setattr(config, "ARTIFACT_READY_FLAG", tmp_path / "artifact_ready.json")
    monkeypatch.setattr(build, "_git_rev", lambda: "smoketest")

    rc = build.build(force=False, quiet=True)
    assert rc == 0
    art = config.ARTIFACT_DIR
    meta = json.loads((art / "metadata.json").read_text())
    assert meta["decision"]["model_kind"] == "keras"
    assert (art / "keras_l1.keras").exists()
    assert (art / "scaler.json").exists()
    assert config.REPORT_PATH.exists()
    assert "DoD-Checkliste" in config.REPORT_PATH.read_text()
    assert json.loads(config.ARTIFACT_READY_FLAG.read_text())["model_kind"] == "keras"
    # prepare_data lieferte die Mini-Store-Transitions (198-6 = 192).
    assert stats.n_transitions == 192


def test_group_mae_missing_cols():
    assert build._group_mae({"a": 1.0}, ("a", "b")) == 1.0
    assert np.isnan(build._group_mae({}, ("a",)))


def test_main_cli_args_parse(capsys):
    with pytest.raises(SystemExit):
        build.main(["build", "--help"])
    out = capsys.readouterr().out
    assert "--force" in out and "--epochs" in out and "--quiet" in out
    # Unknown-Command-Path (nur „build“ ist implementiert).
    with pytest.raises(SystemExit) as ei:
        build.main(["metrics"])
    assert "Unknown command" in str(ei.value)
