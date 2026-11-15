"""Tests: Determinismus (zwei Läufe Mini-Modell → identische Prädikate;
LGBM deterministisch; Seed-Konstruktion)."""
from __future__ import annotations

import numpy as np

from analysis.model.m2 import config, model

from .conftest import make_mini_keras, make_mini_lgbm, train_mini_keras


def _data(mini_store):
    splits, stats, X_ext, flags, scaler = mini_store
    X_tr = scaler.transform(X_ext["train"])
    X_va = scaler.transform(X_ext["valid"])
    Y_tr = splits["train"].Y
    Y_va = splits["valid"].Y
    return X_tr, X_va, Y_tr, Y_va


def test_keras_two_runs_identical_predictions(mini_store):
    """Zwei vollständige (deterministische) Mini-Trainings-Läufe →
    **identische** Prädikate (Seed 20261031, CPU, oneDNN-OFF)."""
    X_tr, X_va, Y_tr, Y_va = _data(mini_store)
    preds = []
    for _ in range(2):
        m = make_mini_keras(X_tr.shape[1])
        train_mini_keras(m, X_tr, Y_tr, X_va, Y_va, epochs=2)
        preds.append(model.predict_targets(m, X_va))
    p1, p2 = preds
    assert p1.shape == p2.shape == (len(Y_va), len(config.TARGETS))
    # Identisch bis float32-Rundung (Batch-Parallelismus determiniert).
    np.testing.assert_allclose(p1, p2, atol=1e-5, rtol=1e-5)


def test_keras_one_epoch_loss_identical_runs(mini_store):
    """Mini-Determinismustest (Keras): zwei unabhängige Trainings-Läufe eines
    kleinen Netzes (gleiche Start-Weights aus Seed, 1 Epoch) → **identische**
    1-Epoch-Trainings-Loss. Basis des Keras-GPU-Determinismus-Checks
    (Seed + TF_CUBLAS_DETERMINISTIC=1 + Device; GPU-Gemms deterministisch
    per cuBLAS-Kernauswahl — CPU-Referenzlauf hier, GPU-Run im Report)."""
    splits, _s, X_ext, _f, scaler = mini_store
    Xtr = scaler.transform(X_ext["train"][:64])
    Xva = scaler.transform(X_ext["valid"][:32])
    Ytr, Yva = splits["train"].Y[:64], splits["valid"].Y[:32]
    losses = []
    for _ in range(2):
        m = make_mini_keras(Xtr.shape[1])
        hist = train_mini_keras(m, Xtr, Ytr, Xva, Yva, epochs=1)
        losses.append(float(hist.history["loss"][-1]))
    assert losses[0] == losses[1]  # exakt gleich (deterministische Kernauswahl)


def test_keras_untrained_predict_deterministic(mini_keras, mini_store):
    """Ohne Training: zwei predict-Aufrufe → exakt identisch (kein
    Stochastik-Element außer Dropout, hier = 0.0)."""
    splits, stats, X_ext, flags, scaler = mini_store
    m = mini_keras
    X = scaler.transform(X_ext["valid"][:32])
    a = __import__("analysis.model.m2.model", fromlist=["predict_targets"]).predict_targets(m, X)
    b = __import__("analysis.model.m2.model", fromlist=["predict_targets"]).predict_targets(m, X)
    np.testing.assert_allclose(a, b, atol=0.0, rtol=0.0)


def test_lgbm_deterministic_two_runs(mini_store):
    """Zwei LGBM-Trainings (deterministic=True, Seed) → identische Prädikate."""
    X_tr, X_va, Y_tr, Y_va = _data(mini_store)
    p = []
    for _ in range(2):
        ens = make_mini_lgbm(X_tr, Y_tr, X_va, Y_va, boost=5)
        p.append(ens.predict_targets(X_va))
    np.testing.assert_allclose(p[0], p[1], atol=0.0, rtol=0.0)


def test_lgbm_params_deterministic_flags():
    from analysis.model.m2 import lgbm_model

    pr = lgbm_model.lgbm_params()
    assert pr["deterministic"] is True
    assert pr["force_row_wise"] is True
    assert pr["objective"] == "regression_l1"
    assert pr["seed"] == config.SEED
