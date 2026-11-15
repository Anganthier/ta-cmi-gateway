"""Tests: Modell-Konstruktion (Shape-Checks mit Mini-Modell, Param-Count
< 500k, Head-Aktivierung, gewichtetes MAE, predict_targets-Shape)."""
from __future__ import annotations

import numpy as np
import pytest

from analysis.model.m2 import config, model


def test_param_count_full_architecture_below_budget():
    """Theoretische Parameter-Count der Design-Architektur (256/128/64 +
    31×(32→1)) muss < 500k sein — ohne Modell bauen (schnell)."""
    n = model._theoretical_param_count(n_features=config.N_FEATURES)
    assert n < config.MAX_PARAMS
    assert n > 100_000  # nicht trivial


def test_mini_model_shape_and_params():
    """Mini-Modell: Input-Shape, 31 Outputs (einer je Target), Param-Count."""
    m = model.build_model(
        config.N_FEATURES,
        targets=config.TARGETS,
        hidden=(16,),
        dropout=0.0,
        head_hidden=4,
    )
    assert len(m.output_shape) == len(config.TARGETS)
    for o in m.output_shape:
        assert o[-1] == 1
    n = model.count_params(m)
    assert 0 < n < config.MAX_PARAMS
    assert model.assert_param_budget(m) == n


def test_head_activation():
    assert model.head_activation("ras_kell_ist") == "linear"
    assert model.head_activation("oel_burner_on") == "sigmoid"
    assert model.head_activation("pump_hz_on") == "sigmoid"
    for t in config.DIGITAL_TARGETS:
        assert model.head_activation(t) == "sigmoid"
    for t in config.ANALOG_TARGETS:
        assert model.head_activation(t) == "linear"


def test_assert_param_budget_raises():
    m = model.build_model(
        config.N_FEATURES, targets=config.TARGETS, hidden=(512, 512), head_hidden=64
    )
    n = model.count_params(m)
    if n > 500_000:
        with pytest.raises(AssertionError):
            model.assert_param_budget(m, max_params=500_000)
    else:
        assert model.assert_param_budget(m, max_params=500_000) == n


def test_weighted_mae_known_values():
    targets = config.TARGETS
    Y_true = np.tile(np.arange(len(targets), dtype=np.float64), (4, 1))
    Y_pred = Y_true + 1.0  # MAE je Target = 1.0
    out = model.weighted_mae(Y_true, Y_pred)
    for t in targets:
        assert abs(out[t] - 1.0) < 1e-9
    assert abs(out["weighted_mean"] - 1.0) < 1e-9  # alle MAE=1 → gewichtet 1
    # Asymmetrische Fehler: nur ein Target falsch (mae=3), alle übrigen mae=0.
    Y_pred2 = Y_true.copy()
    i = targets.index("ras_kell_ist")
    Y_pred2[:, i] = Y_true[:, i] + 3.0
    out2 = model.weighted_mae(Y_true, Y_pred2)
    assert abs(out2[targets[i]] - 3.0) < 1e-9
    # Gewichtetes MAE = Σ w·mae / Σ w → nur das eine fehlerhafte Target trägt:
    # 3,0 · w_kell / Σw  (klein < 3,0, da alle übrigen Targets exakt liegen).
    wsum = sum(config.LOSS_WEIGHTS.values())
    expected_wm = 3.0 * config.LOSS_WEIGHTS[targets[i]] / wsum
    assert abs(out2["weighted_mean"] - expected_wm) < 1e-9
    assert 0.0 < out2["weighted_mean"] < 3.0


def test_predict_targets_shape(mini_keras, mini_store):
    """Multi-Output-Keras → (n, n_targets) (nicht (n, n_targets, 1))."""
    splits, stats, X_ext, flags, scaler = mini_store
    m = mini_keras
    X = scaler.transform(X_ext["train"][:32])
    p = model.predict_targets(m, X)
    assert p.shape == (32, len(config.TARGETS))
    assert np.isfinite(p).all()


def test_build_model_deterministic_weights(mini_store):
    """Zwei aufeinanderfolgende Konstruktionen (Seed) → identische
    (Initial-)Weights (Determinismus-Konstruktion)."""
    splits, stats, X_ext, flags, scaler = mini_store
    m1 = model.build_model(8, targets=config.TARGETS[:4], hidden=(8,), head_hidden=4)
    m2 = model.build_model(8, targets=config.TARGETS[:4], hidden=(8,), head_hidden=4)
    assert len(m1.weights) == len(m2.weights)
    for a, b in zip(m1.weights, m2.weights, strict=False):
        np.testing.assert_allclose(np.array(a), np.array(b), atol=0.0, rtol=0.0)


def test_weighted_mae_loss_scales_by_target_weight():
    """_weighted_mae_loss: MAE × Gewichtung (Design: MAE je Target, gewichtet)."""
    yt = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    yp = np.array([2.0, 2.0, 4.0], dtype=np.float32)  # MAE = 2/3
    for w in (0.4, 0.8, 1.0):
        loss = model._weighted_mae_loss(w)
        out = loss(yt, yp)
        assert abs(float(out) - (2.0 / 3.0) * w) < 1e-6
    # build_model kompiliert mit pro-Head-Gewichtung (verlustfrei prüfbar:
    # Loss-Dict-Keys = head_{t}).
    m = model.build_model(4, targets=config.TARGETS[:3], hidden=(4,), head_hidden=2)
    losses = m.loss  # Keras 3: {head_name: loss}
    assert set(losses) == {f"head_{t}" for t in config.TARGETS[:3]}


def test_train_l1_mini(mini_store, mini_keras):
    """train_l1 (Public-Helper): Mini-Training (2 Epochs) → History +
    best_metrics (gewichtetes MAE-Dict mit „weighted_mean“)."""
    splits, _s, X_ext, _f, scaler = mini_store
    Xtr = scaler.transform(X_ext["train"][:64])
    Xva = scaler.transform(X_ext["valid"][:32])
    history, best = model.train_l1(
        mini_keras, Xtr, splits["train"].Y[:64], Xva, splits["valid"].Y[:32],
        epochs=2, patience=1,
    )
    assert "loss" in history["history"] and "val_loss" in history["history"]
    assert len(history["history"]["loss"]) == 2
    assert "weighted_mean" in best
    for t in config.TARGETS:
        assert t in best
