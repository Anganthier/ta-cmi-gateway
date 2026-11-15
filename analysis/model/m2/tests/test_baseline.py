"""Tests: B2-Naive (Hand-Check mit kleinen Arrays) + Rollout-Truth-
Semantik (1-h-Modell → 4-h-Rollout, Persistenz/Gestern-Index)."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from analysis.model.m2 import baseline, config
from analysis.model.m2.transitions import Split


def _mini_split(n=40, start=None, seed=0):
    """Lückenloses Stunden-Raster (naive Berlin), Y = bekannte Funktion."""
    start = start or datetime(2025, 2, 1)
    ts = [start + timedelta(hours=i) for i in range(n)]
    rng = np.random.default_rng(seed)
    Y = rng.normal(20.0, 1.0, size=(n, config.N_TARGETS)).astype(np.float32)
    # Ein Target = bekannte Funktion der Zeit (für Hand-Checks):
    # Y[:,0] = 10 + (i mod 24)·0.5  → „gleiche Zeit gestern" ist exakt testbar.
    Y[:, 0] = 10.0 + (np.arange(n) % 24) * 0.5
    X = rng.normal(0.0, 1.0, size=(n, 4)).astype(np.float32)
    return Split(
        name="valid",
        X=X,
        Y=Y,
        ts=ts,
        feature_null_mask=np.zeros((n, 4), dtype="uint8"),
        target_null_mask=np.zeros((n, config.N_TARGETS), dtype="uint8"),
    )


def test_b2_step_hand_check():
    """B2-1-Step = 0,5·Persistenz + 0,5·gleiche Zeit gestern (pro Target)."""
    persist = np.array([10.0, 20.0, 30.0])
    yest = np.array([12.0, 24.0, 36.0])
    out = baseline.b2_step(persist, yest)
    np.testing.assert_allclose(out, [11.0, 22.0, 33.0])
    # Identische Inputs → Persistenz = B2 = Wert.
    p = np.array([5.0, 5.0])
    np.testing.assert_allclose(baseline.b2_step(p, p), [5.0, 5.0])


def test_b2_step_shape_mismatch_raises():
    with pytest.raises(ValueError):
        baseline.b2_step(np.zeros(3), np.zeros(4))


def test_b2_4h_series_semantics():
    """B2-4-h-Rollout: Step 0 = 0,5·Y[i−1] + 0,5·Y[j−1] (j = t_{i+1}−24h),
    Step k = 0,5·Prädikt_{k−1} + 0,5·Y[j−1] (gestern konstant)."""
    split = _mini_split(n=48)
    # i = 30 → t_i = 2025-02-01T30:00 = 2025-02-02T06:00; Ziel t_{i+1}=07:00.
    # Persistenz = Y[29]; gestern = Wert von (07:00 − 24h) = 06:00 → Zeile j mit
    # ts=06:00 = Index 5 → Wert = Y[4] (Y[j−1]).
    i = 30
    Y = split.Y
    ts_map = {ts: k for k, ts in enumerate(split.ts)}
    j = ts_map[split.ts[i] + timedelta(hours=1) - timedelta(hours=24)]
    expected0 = 0.5 * Y[i - 1] + 0.5 * Y[j - 1]
    pred, n_excluded = baseline.b2_4h_series(split, np.array([i]), steps=4)
    np.testing.assert_allclose(pred[0, 0], expected0, atol=1e-5)
    # Step 1 = 0,5·Step0 + 0,5·Y[j−1] (gestern konstant).
    expected1 = 0.5 * expected0 + 0.5 * Y[j - 1]
    np.testing.assert_allclose(pred[0, 1], expected1, atol=1e-5)
    assert n_excluded == 0


def test_rollout_truth_indexing():
    """Rollout-Truth: Start i → k-Step (k=0…steps−1) = Wert t_{i+1+k} = Y[i+k]."""
    split = _mini_split(n=20)
    i = 3
    truth = baseline.rollout_truth(split, np.array([i]), steps=4)
    for k in range(4):
        np.testing.assert_allclose(truth[0, k], split.Y[i + k], atol=1e-5)


def test_rollout_start_indices_boundaries():
    split = _mini_split(n=10)
    starts = baseline.rollout_start_indices(split, steps=4)
    # Start i nutzt Y[i … i+steps−1]; letzter Start = n − steps (i+steps−1 =
    # n−1 im Raster) → Indizes 0 … 6 (7 Starts für n=10, steps=4).
    assert starts[0] == 0
    assert starts[-1] == 10 - 4
    assert len(starts) == 10 - 4 + 1
    truth = baseline.rollout_truth(split, starts, steps=4)
    assert not np.isnan(truth).any()


def test_mae_nan_handling():
    pred = np.array([1.0, 2.0, 3.0])
    truth = np.array([1.0, np.nan, 3.0])
    # NaN-Zeile ausgeschlossen → MAE über [1,1] = 0.
    assert baseline.mae(pred, truth) == 0.0
    # Alle NaN → NaN.
    assert np.isnan(baseline.mae(np.full(3, np.nan), np.full(3, np.nan)))


def test_mae_per_target():
    pred = np.array([[0.0], [2.0], [4.0]])
    truth = np.array([[1.0], [1.0], [1.0]])
    out = baseline.mae_per_target(pred, truth)
    # |0-1| + |2-1| + |4-1| = 5 → MAE = 5/3.
    np.testing.assert_allclose(out, [5.0 / 3.0])
