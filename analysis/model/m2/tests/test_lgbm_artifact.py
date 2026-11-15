"""Tests: LightGBM (Parameter, Save/Load-Roundtrip, Predict-Shape) und
Artefakt-Bau (Keras + LGBM + Scaler + metadata + README)."""
from __future__ import annotations

import numpy as np

from analysis.model.m2 import config, lgbm_model

from .conftest import make_mini_keras, make_mini_lgbm


def test_lgbm_predict_shape_and_finite(mini_store):
    splits, stats, X_ext, flags, scaler = mini_store
    ens = make_mini_lgbm(
        scaler.transform(X_ext["train"][:200]),
        splits["train"].Y[:200],
        scaler.transform(X_ext["valid"][:80]),
        splits["valid"].Y[:80],
        boost=4,
    )
    p = ens.predict_targets(scaler.transform(X_ext["valid"][:80]))
    assert p.shape == (80, len(config.TARGETS))
    assert np.isfinite(p).all()


def test_lgbm_save_load_roundtrip(mini_store, tmp_path):
    splits, stats, X_ext, flags, scaler = mini_store
    ens = make_mini_lgbm(
        scaler.transform(X_ext["train"][:200]),
        splits["train"].Y[:200],
        scaler.transform(X_ext["valid"][:80]),
        splits["valid"].Y[:80],
        boost=4,
    )
    out = tmp_path / "lgbm_l1"
    paths = ens.save(out)
    assert len(paths) == len(config.TARGETS)
    for t in config.TARGETS:
        assert paths[t].name == f"{t}.txt"
        assert paths[t].exists()
    ens2 = lgbm_model.LGBMEnsemble.load(out, targets=config.TARGETS)
    X = scaler.transform(X_ext["valid"][:80])
    np.testing.assert_allclose(ens.predict_targets(X), ens2.predict_targets(X), atol=1e-6, rtol=1e-6)


def test_build_artifact_structure(mini_store, tmp_path):
    """build_artifact: alle Artefakt-Dateien existieren + metadata konsistent."""
    from analysis.model.m2 import artifact

    splits, stats, X_ext, flags, scaler = mini_store
    n_feat = X_ext["train"].shape[1]
    m = make_mini_keras(n_feat)
    ens = make_mini_lgbm(
        scaler.transform(X_ext["train"][:200]),
        splits["train"].Y[:200],
        scaler.transform(X_ext["valid"][:80]),
        splits["valid"].Y[:80],
        boost=4,
    )
    from analysis.model.m2.model import count_params

    out = tmp_path / "cmi-brain" / "v001"
    maes = {"keras_valid_1h": {t: 0.5 for t in config.TARGETS}}
    decision = {
        "model_kind": "keras",
        "reason": "Test",
        "keras_score": 0.5,
        "lightgbm_score": 0.6,
    }
    split_info = {
        "train_start": "2020-08-31",
        "train_end": "2024-12-31",
        "valid_start": "2025-01-01",
        "valid_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-10-31",
    }
    d = artifact.build_artifact(
        keras_model=m,
        lgbm_ens=ens,
        scaler=scaler,
        keras_params=count_params(m),
        maes=maes,
        decision=decision,
        split_info=split_info,
        framework_versions={"tensorflow": "2.21", "keras": "3.15", "lightgbm": "4.7"},
        n_features=n_feat,
        out_dir=out,
    )
    assert (d / "keras_l1.keras").exists()
    assert (d / "scaler.json").exists()
    assert (d / "metadata.json").exists()
    assert (d / "README.md").exists()
    assert len(list((d / "lgbm_l1").glob("*.txt"))) == len(config.TARGETS)
    import json

    meta = json.loads((d / "metadata.json").read_text())
    assert meta["data_version"] == config.DATA_VERSION
    assert meta["seed"] == config.SEED
    assert meta["decision"]["model_kind"] == "keras"
    assert meta["n_features"] == n_feat
    assert meta["param_count"] == count_params(m)
    assert meta["frameworks"]["keras"] == "3.15"
    # Scaler-Roundtrip aus Artefakt.
    from analysis.model.m2.normalize import StandardScaler

    sc = StandardScaler.load_json(d / "scaler.json")
    assert sc.n_features == n_feat


def test_artifact_metadata_git_rev_and_split_info(mini_store, tmp_path):
    from analysis.model.m2 import artifact

    splits, stats, X_ext, flags, scaler = mini_store
    m = make_mini_keras(X_ext["train"].shape[1])
    ens = make_mini_lgbm(
        scaler.transform(X_ext["train"][:100]),
        splits["train"].Y[:100],
        scaler.transform(X_ext["valid"][:40]),
        splits["valid"].Y[:40],
        boost=2,
    )
    from analysis.model.m2.model import count_params

    out = artifact.build_artifact(
        keras_model=m,
        lgbm_ens=ens,
        scaler=scaler,
        keras_params=count_params(m),
        maes={},
        decision={"model_kind": "lightgbm", "reason": "x"},
        split_info={
            "train_start": "2020-08-31T00:00",
            "train_end": "2024-12-31T23:00",
            "valid_start": "2025-01-01T00:00",
            "valid_end": "2025-12-31T23:00",
            "test_start": "2026-01-01T00:00",
            "test_end": "2026-10-31T12:00",
        },
        framework_versions={"tensorflow": "t", "keras": "k", "lightgbm": "l"},
        n_features=X_ext["train"].shape[1],
        out_dir=tmp_path / "v",
    )
    import json

    meta = json.loads((out / "metadata.json").read_text())
    assert len(meta["train_end"]) == 10  # YMD
    assert meta["valid_period"] == ["2025-01-01", "2025-12-31"]
    assert meta["test_period"][0] == "2026-01-01"
    assert isinstance(meta["git_rev"], str) and meta["git_rev"]
