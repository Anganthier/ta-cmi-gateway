"""Tests: Normalisierung (Round-Trip, Train-Fit-Nur, Serialisierung)."""
from __future__ import annotations

import numpy as np
import pytest

from analysis.model.m2 import normalize


def test_roundtrip_residual_small():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 40)).astype(np.float32)
    sc = normalize.StandardScaler.fit(X)
    resid = sc.roundtrip_residual(X)
    assert resid < 1e-3  # float32-Präzision


def test_transform_uses_train_stats_only():
    """Train-Fit-Nur: Mean/Std müssen exakt die **Train**-Spaltenstatistiken
    sein (kein Valid/Test-Leak in die Normalisierung)."""
    rng = np.random.default_rng(1)
    X_tr = rng.normal(5.0, 2.0, size=(500, 10)).astype(np.float32)
    X_va = rng.normal(0.0, 0.5, size=(200, 10)).astype(np.float32)  # andere Statistik
    sc = normalize.StandardScaler.fit(X_tr)
    np.testing.assert_allclose(sc.mean_, X_tr.mean(axis=0), atol=1e-4)
    np.testing.assert_allclose(sc.std_, np.maximum(np.sqrt(X_tr.var(axis=0)), 1e-4), atol=1e-4)
    # transform des Valid muss die Train-Statistik anwenden (≠ Valid-Mean).
    z = sc.transform(X_va)
    np.testing.assert_allclose(z.mean(axis=0), (X_va.mean(axis=0) - sc.mean_) / sc.std_, atol=1e-4)
    assert sc.n_samples_seen == 500


def test_constant_column_stable():
    """Konstante Spalte (z. B. mode_known) → std = _STD_MIN, keine Division
    durch ~0, transform/inverse bleibt endlich."""
    X = np.zeros((50, 3), dtype=np.float32)
    X[:, 1] = 3.0
    sc = normalize.StandardScaler.fit(X)
    assert np.isfinite(sc.std_).all()
    assert sc.std_.min() >= 1e-4
    z = sc.transform(X)
    np.testing.assert_allclose(sc.inverse_transform(z), X, atol=1e-4)


def test_feature_count_mismatch_raises():
    X = np.random.default_rng(2).normal(size=(10, 5)).astype(np.float32)
    sc = normalize.StandardScaler.fit(X)
    with pytest.raises(ValueError):
        sc.transform(np.zeros((3, 4), dtype=np.float32))


def test_json_roundtrip(tmp_path):
    X = np.random.default_rng(3).normal(size=(100, 12)).astype(np.float32)
    sc = normalize.StandardScaler.fit(X)
    p = tmp_path / "scaler.json"
    sc.save_json(p)
    sc2 = normalize.StandardScaler.load_json(p)
    np.testing.assert_array_equal(sc.mean_, sc2.mean_)
    np.testing.assert_array_equal(sc.std_, sc2.std_)
    assert sc2.n_features == 12
    z1 = sc.transform(X[:5])
    z2 = sc2.transform(X[:5])
    np.testing.assert_allclose(z1, z2, atol=0.0)
    d = sc.to_dict()
    assert d["kind"] == "standard_scaler"
    assert len(d["mean"]) == 12


def test_from_dict_rejects_wrong_kind():
    with pytest.raises(ValueError):
        normalize.StandardScaler.from_dict({"kind": "other"})


def test_constant_flag_unseen_one_is_bounded():
    """Bugfix M2b-P0: Im Train konstant-0-Flag, das in Valid/Test 1 wird,
    darf nicht auf z = 10.000 explodieren (früher std = 1e-4)."""
    X = np.zeros((100, 2), dtype=np.float32)
    X[:, 0] = np.random.default_rng(4).normal(size=100)
    sc = normalize.StandardScaler.fit(X)
    z = sc.transform(np.array([[0.0, 1.0]], dtype=np.float32))
    assert abs(float(z[0, 1])) <= 1.0 + 1e-6  # nur zentriert (std = 1)


def test_legacy_scaler_std_min_is_treated_as_constant():
    """v001-``scaler.json`` speichert std = 1e-4 für konstante Spalten → wird
    beim Laden als std = 1 interpretiert (kompatibel, keine Explosion)."""
    sc = normalize.StandardScaler.from_dict(
        {"kind": "standard_scaler", "n_features": 2, "n_samples_seen": 10,
         "mean": [0.0, 0.0], "std": [2.0, 1e-4]}
    )
    z = sc.transform(np.array([[4.0, 1.0]], dtype=np.float32))
    np.testing.assert_allclose(z, [[2.0, 1.0]], atol=1e-6)
    np.testing.assert_allclose(sc.inverse_transform(z), [[4.0, 1.0]], atol=1e-5)


def test_transform_clips_extreme_z():
    X = np.random.default_rng(5).normal(size=(500, 1)).astype(np.float32)
    sc = normalize.StandardScaler.fit(X)
    z = sc.transform(np.array([[1e6]], dtype=np.float32))
    assert float(z[0, 0]) == normalize.CLIP_Z
    z_raw = sc.transform(np.array([[1e6]], dtype=np.float32), clip=None)
    assert float(z_raw[0, 0]) > 1e5
