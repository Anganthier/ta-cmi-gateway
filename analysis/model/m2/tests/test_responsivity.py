"""Tests: Offset-Responsivitäts-Test-Logik (synthetisch: simuliertes
Mini-Modell, das Offset-Input bekanntlich „bestraft“ → Test-Pipeline muss
GREEN sein + korrekte Vorzeichen-Erkennung: Offset↑ → ΔH<0, ΔD<0)."""
from __future__ import annotations

import numpy as np

from analysis.model.m2 import config, responsivity
from analysis.model.m2.rollout import rollout_matrix


class _PunishingOffsetFn:
    """Simulierte Anlagen-Dynamik (Batch-pred_fn): Target-Prädikate, die
    den **Input**-Keller-Offset (Raw-Spalte ``offset_kell``) bestrafen:
    - ``heat_min_kell`` (Target-Index 19) = -0.30 · (offset_kell − 1.0) + 5.9
      → Offset↑ → Heizminuten↓ (18.10-Kausalrichtung),
    - ``pump_hz_on`` (dev0 d8, Target-Index 23) = 0.90 − 0.08·(offset_kell−1.0)
      → Offset↑ → Pumpe weniger an.
    Alle übrigen Targets = Konstante (neutral)."""

    def __init__(self, offset_idx: int, targets: tuple[str, ...]):
        self.offset_idx = offset_idx
        self.targets = targets
        self.i_hm = targets.index("heat_min_kell")
        self.i_d8 = targets.index("pump_hz_on")

    def __call__(self, X: np.ndarray) -> np.ndarray:
        n = len(X)
        P = np.zeros((n, len(self.targets)), dtype=np.float64)
        off = X[:, self.offset_idx]
        P[:, self.i_hm] = -0.30 * (off - 1.0) + 5.9
        P[:, self.i_d8] = 0.90 - 0.08 * (off - 1.0)
        return P


def _offset_idx_for(n_features: int) -> int:
    """Raw-Spaltenindex von ``offset_kell`` in der Extended-Matrix
    (Features zuerst, Flags danach)."""
    return config.FEATURE_INPUTS.index("offset_kell")


def test_responsivity_sign_detection_positive(mini_store):
    """Synthetischer Offset-Pfaff: Korrelation(Δoffset, ΔH) muss ≤ −0.6 sein
    UND Anteil ΔH<0 bei +3,4 K = 100 % → DoD-Pipeline erkennt GREEN."""
    splits, stats, X_ext, flags, scaler = mini_store
    valid = splits["valid"]
    targets = config.TARGETS
    n_feat = valid.X.shape[1]
    fn = _PunishingOffsetFn(_offset_idx_for(n_feat), targets)
    case_idx = responsivity.select_cases(valid, n_cases=30, seed=config.SEED, horizon_h=6)
    # Hold-Run: offset_kell fixiert = t0 (Δ=0) → „kein“ Step.
    # Step-Runs: offset_kell fixiert = t0 + Δ.
    R = valid.X[case_idx]
    fixed_hold = np.full(R.shape, np.nan)
    fixed_hold[:, _offset_idx_for(n_feat)] = R[:, _offset_idx_for(n_feat)]
    hold = rollout_matrix(R, fn, steps=6, fixed_mat=fixed_hold, targets=targets)
    step_preds = {}
    for d in config.RESP_DELTAS_K:
        fixed = np.full(R.shape, np.nan)
        fixed[:, _offset_idx_for(n_feat)] = R[:, _offset_idx_for(n_feat)] + d
        step_preds[d] = rollout_matrix(R, fn, steps=6, fixed_mat=fixed, targets=targets)
    res = responsivity.compute_result(hold, step_preds, config.RESP_DELTAS_K, targets)
    # Vorzeichen: ΔH < 0 für alle Δ (Offset↑ → heat↓).
    for d in config.RESP_DELTAS_K:
        assert res.per_delta[d]["mean_dH"] < 0.0
        assert res.per_delta[d]["frac_dH_neg"] == 1.0
        assert res.per_delta[d]["mean_dD"] < 0.0
    # Korrelation (4 Δ-Level) stark negativ.
    assert res.corr_dH <= -0.9
    assert res.dod_corr_ok
    assert res.dod_frac >= 0.75
    assert res.dod_frac_ok
    assert res.passed
    assert res.n_cases == 30


def test_responsivity_summary_shape(mini_store):
    splits, stats, X_ext, flags, scaler = mini_store
    valid = splits["valid"]
    targets = config.TARGETS
    n_feat = valid.X.shape[1]
    fn = _PunishingOffsetFn(_offset_idx_for(n_feat), targets)
    case_idx = responsivity.select_cases(valid, n_cases=10, seed=config.SEED, horizon_h=6)
    R = valid.X[case_idx]
    fixed_hold = np.full(R.shape, np.nan)
    fixed_hold[:, _offset_idx_for(n_feat)] = R[:, _offset_idx_for(n_feat)]
    hold = rollout_matrix(R, fn, steps=6, fixed_mat=fixed_hold, targets=targets)
    step_preds = {}
    for d in config.RESP_DELTAS_K:
        fixed = np.full(R.shape, np.nan)
        fixed[:, _offset_idx_for(n_feat)] = R[:, _offset_idx_for(n_feat)] + d
        step_preds[d] = rollout_matrix(R, fn, steps=6, fixed_mat=fixed, targets=targets)
    res = responsivity.compute_result(hold, step_preds, config.RESP_DELTAS_K, targets)
    s = res.summary()
    assert s["n_runs"] == 10 * 4
    assert "pearson_delta_offset_dH" in s
    assert "dH_mittel_je_delta" in s
    assert s["dod_passed"] is True


def test_select_cases_seed_reproducible_and_gapfree(mini_store):
    splits, stats, X_ext, flags, scaler = mini_store
    valid = splits["valid"]
    i1 = responsivity.select_cases(valid, n_cases=20, seed=config.SEED, horizon_h=6)
    i2 = responsivity.select_cases(valid, n_cases=20, seed=config.SEED, horizon_h=6)
    np.testing.assert_array_equal(i1, i2)
    i3 = responsivity.select_cases(valid, n_cases=20, seed=config.SEED + 1, horizon_h=6)
    assert not (i1 == i3).all()  # Seed wirkt
    # „Lückenfrei": keine NULL-Features/Targets in den gewählten Rows +
    # Horizon-Abstand zum Split-Ende.
    n = len(valid.ts)
    assert (i1 < n - 6).all()
    for i in i1:
        assert int(valid.feature_null_mask[i].sum()) == 0
        assert int(valid.target_null_mask[i].sum()) == 0
    assert len(i1) == 20


def test_select_cases_respects_null_rows(mini_store):
    """Valid-Row 65 (gap-Drop → keine Transition) kann nicht gewählt werden;
    NULL-besetzte Rows (solar_coll fehlt erst in Train) → im Valid keine
    NULL-Rows → Auswahl deckt nur gültige Indices ab."""
    splits, stats, X_ext, flags, scaler = mini_store
    valid = splits["valid"]
    idx = responsivity.select_cases(valid, n_cases=50, seed=config.SEED, horizon_h=6)
    assert len(idx) == 50
    assert (idx >= 0).all() and (idx < len(valid.ts)).all()
