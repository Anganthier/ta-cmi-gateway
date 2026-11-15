"""M2 — Offset-Responsivitäts-Test (Kern-DoD, 18.10-Muster).

Design (M2-Spec, Design v0.3 §3.1):
- **200 zufällige, lückenfreie Valid-Zustände** (Seed 20261031) ×
  **Offset-Steps Δ ∈ {+1,0; +2,0; +3,4; +5,0 K} (nur Keller)**.
- **6-h-Rollout** mit Offset-Halt vs Offset+Δ (Keller-Offset = t0-Wert bzw.
  t0-Wert + Δ; alle übrigen Aktionen/Offsets = t0-Werte → reiner
  Offset-Kontrastfakt, dokumentiert).
- **ΔH** = Summe ``heat_min_kell`` (0–6 h) im Δ-Fall minus im Hold-Fall.
- **ΔD** = An-Quotient ``pump_hz_on`` (dev0 d8, Mittel über 0–6 h) Differenz.
- **DoD (hart)**:
  1. ``Pearson(Δoffset, ΔH) ≤ −0,6`` (Offset↑ → Heizminuten↓),
  2. bei **+3,4 K** (18.10-Wert): ``ΔH < 0`` in **≥ 75 %** der 200 Fälle.

Ergebnis (Korrelation, Anteil, Δ-Mittel) wird in REPORT.md dokumentiert.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .model import predict_targets
from .transitions import Split


@dataclass
class ResponsivityResult:
    """Ergebnis des Offset-Responsivitäts-Tests."""

    n_cases: int
    n_deltas: int
    deltas: tuple[float, ...]
    corr_dH: float  # Pearson(Δoffset, ΔH) über alle (Falls, Δ)-Paare
    corr_dD: float  # Pearson(Δoffset, ΔD)
    per_delta: dict[float, dict[str, float]] = field(default_factory=dict)
    dod_corr_ok: bool = False  # corr_dH <= -0.6
    dod_frac: float = 0.0  # Anteil ΔH<0 bei Δ=3,4
    dod_frac_ok: bool = False  # >= 0.75
    passed: bool = False

    @property
    def target_delta(self) -> float:
        return config.RESP_TARGET_DELTA

    def summary(self) -> dict[str, object]:
        return {
            "n_cases": self.n_cases,
            "n_runs": int(self.n_cases * self.n_deltas),
            "deltas_k": list(self.deltas),
            "pearson_delta_offset_dH": round(self.corr_dH, 4),
            "pearson_delta_offset_dD": round(self.corr_dD, 4),
            "dH_mittel_je_delta": {
                f"+{d:.1f}K": round(v["mean_dH"], 4) for d, v in self.per_delta.items()
            },
            "dD_mittel_je_delta": {
                f"+{d:.1f}K": round(v["mean_dD"], 4) for d, v in self.per_delta.items()
            },
            "dH_neg_anteil_3.4K": round(self.dod_frac, 4),
            "dod_corr_ok": self.dod_corr_ok,
            "dod_frac_ok": self.dod_frac_ok,
            "dod_passed": self.passed,
        }


def select_cases(
    valid: Split,
    n_cases: int = config.RESP_N_CASES,
    seed: int = config.SEED,
    horizon_h: int = config.RESP_HORIZON_H,
) -> np.ndarray:
    """Wählt ``n_cases`` lückenfreie Valid-Indizes (Seed fest).

    „Lückenfrei" = keine NULL-Features (``feature_null_mask``) und keine
    NULL-Targets (``target_null_mask``) in der Ausgangs-Row; zusätzlich
    ``horizon_h`` Zeilen Abstand zum Split-Ende (Rollout-Ziel-Rows).
    """
    rng = np.random.default_rng(seed)
    n = len(valid.ts)
    eligible = [
        i
        for i in range(n - horizon_h)
        if int(valid.feature_null_mask[i].sum()) == 0
        and int(valid.target_null_mask[i].sum()) == 0
    ]
    if not eligible:
        raise ValueError("keine lückenfreien Valid-Zustände gefunden")
    size = int(min(n_cases, len(eligible)))
    idx = rng.choice(np.asarray(eligible, dtype=np.int64), size=size, replace=False)
    return np.sort(idx)


def compute_result(
    hold_preds: np.ndarray,
    step_preds: dict[float, np.ndarray],
    deltas: tuple[float, ...],
    targets: tuple[str, ...] = config.TARGETS,
) -> ResponsivityResult:
    """ΔH/ΔD + Korrelationen aus fertigen Rollout-Prädikaten.

    Parameters
    ----------
    hold_preds:
        (n_cases, horizon, n_targets) — Offset-Halt-Rollouts.
    step_preds:
        ``{delta: (n_cases, horizon, n_targets)}`` — Offset-Step-Rollouts.
    """
    i_hm = targets.index("heat_min_kell")
    i_d8 = targets.index("pump_hz_on")

    all_delta: list[float] = []
    all_dH: list[float] = []
    all_dD: list[float] = []
    per_delta: dict[float, dict[str, float]] = {}
    for d in deltas:
        sp = step_preds[d]
        dH = sp[:, :, i_hm].sum(axis=1) - hold_preds[:, :, i_hm].sum(axis=1)
        dD = sp[:, :, i_d8].mean(axis=1) - hold_preds[:, :, i_d8].mean(axis=1)
        all_delta.extend([d] * len(dH))
        all_dH.extend(dH.tolist())
        all_dD.extend(dD.tolist())
        per_delta[d] = {
            "n": int(len(dH)),
            "mean_dH": float(np.mean(dH)),
            "mean_dD": float(np.mean(dD)),
            "frac_dH_neg": float(np.mean(dH < 0)),
            "frac_dD_neg": float(np.mean(dD < 0)),
        }
    d_arr = np.asarray(all_delta, dtype=np.float64)
    dh_arr = np.asarray(all_dH, dtype=np.float64)
    dd_arr = np.asarray(all_dD, dtype=np.float64)
    corr_dh = float(np.corrcoef(d_arr, dh_arr)[0, 1]) if len(set(all_delta)) > 1 and np.std(dh_arr) > 0 else float("nan")
    corr_dd = float(np.corrcoef(d_arr, dd_arr)[0, 1]) if len(set(all_delta)) > 1 and np.std(dd_arr) > 0 else float("nan")

    target_d = config.RESP_TARGET_DELTA
    frac = float(per_delta[target_d]["frac_dH_neg"])
    dod_corr_ok = bool(not np.isnan(corr_dh) and corr_dh <= -0.6)
    dod_frac_ok = bool(frac >= 0.75)
    return ResponsivityResult(
        n_cases=int(len(hold_preds)),
        n_deltas=len(deltas),
        deltas=tuple(deltas),
        corr_dH=corr_dh,
        corr_dD=corr_dd,
        per_delta=per_delta,
        dod_corr_ok=dod_corr_ok,
        dod_frac=frac,
        dod_frac_ok=dod_frac_ok,
        passed=bool(dod_corr_ok and dod_frac_ok),
    )


def _fixed_matrix(
    X_rows: np.ndarray,
    delta: float | None,
) -> np.ndarray:
    """Buildet die (n_cases, n_features) „fixed"-Matrix für einen
    Hold- bzw. Step-Run: alle Digital-Actions + Offsets = t0-Werte; bei
    ``delta != None`` zusätzlich ``offset_kell += delta`` (Step-Run)."""
    from .rollout import feature_names

    n_cases, n_features = X_rows.shape
    fixed = np.full((n_cases, n_features), np.nan, dtype=np.float64)
    names = feature_names(n_features)
    name_idx = {n: i for i, n in enumerate(names)}
    for n in config.DIGITAL_ACTION_INPUTS:
        if n in name_idx:
            fixed[:, name_idx[n]] = X_rows[:, name_idx[n]]
    for n in config.OFFSET_INPUTS:
        if n in name_idx:
            fixed[:, name_idx[n]] = X_rows[:, name_idx[n]]
    if delta is not None and "offset_kell" in name_idx:
        fixed[:, name_idx["offset_kell"]] += delta
    return fixed


def _run_rollouts(
    pred_holder_factory,
    valid: Split,
    case_indices: np.ndarray,
    deltas: tuple[float, ...],
    horizon: int,
    targets: tuple[str, ...],
    batch_size: int = 2048,
) -> tuple[np.ndarray, dict[float, np.ndarray]]:
    """Führt Hold- + Step-Rollouts **batcht** aus (Matrix-Rollout).

    ``pred_holder_factory(fixed_mat) -> pred_fn(X_batch)``: liefert den
    Batch-Prädiktor (Keras: ``model.predict``; LGBM: ``ens.predict_targets``).
    """
    from .rollout import rollout_matrix
    from .transitions import extended_raw

    n_cases = len(case_indices)
    n_targets = len(targets)
    X_rows = extended_raw(valid)[np.asarray(case_indices, dtype=np.int64)]

    hold_fixed = _fixed_matrix(X_rows, delta=None)
    step_fixed = {d: _fixed_matrix(X_rows, delta=d) for d in deltas}

    hold_preds = np.zeros((n_cases, horizon, n_targets), dtype=np.float64)
    step_preds = {d: np.zeros((n_cases, horizon, n_targets), dtype=np.float64) for d in deltas}

    all_runs: list[tuple[np.ndarray, np.ndarray]] = [
        (hold_fixed, hold_preds)
    ] + [(step_fixed[d], step_preds[d]) for d in deltas]
    for fixed_mat, store in all_runs:
        for lo in range(0, n_cases, batch_size):
            hi = min(lo + batch_size, n_cases)
            Xb = X_rows[lo:hi]
            fixed_b = fixed_mat[lo:hi]

            def _pred(xb: np.ndarray, _fb: np.ndarray = fixed_b) -> np.ndarray:
                return pred_holder_factory(_fb)(xb)

            out = rollout_matrix(
                Xb, _pred, steps=horizon, fixed_mat=fixed_b, targets=targets
            )
            store[lo:hi] = out
    return hold_preds, step_preds


def run_responsivity_keras(
    model,
    scaler,
    valid: Split,
    n_cases: int = config.RESP_N_CASES,
    seed: int = config.SEED,
    deltas: tuple[float, ...] = config.RESP_DELTAS_K,
    horizon: int = config.RESP_HORIZON_H,
    targets: tuple[str, ...] | None = None,
    batch_size: int = 2048,
) -> tuple[ResponsivityResult, np.ndarray]:
    """Keras-Offset-Responsivitäts-Test (s. Modul-Doku)."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    case_idx = select_cases(valid, n_cases=n_cases, seed=seed, horizon_h=horizon)

    def factory(fixed_b: np.ndarray):
        del fixed_b  # Fixierung geschieht in rollout_matrix (State-Update).

        def _pred(xb: np.ndarray) -> np.ndarray:
            # predict_targets (Shared-Helfer) stapelt die Keras-3-Liste von
            # (n,1)-Arrays korrekt zu (n, n_targets) (TARGETS-Reihenfolge).
            return predict_targets(model, scaler.transform(xb), batch_size=len(xb))

        return _pred

    hold_p, step_p = _run_rollouts(
        factory, valid, case_idx, deltas, horizon, targets, batch_size=batch_size
    )
    result = compute_result(hold_p, step_p, deltas, targets)
    return result, case_idx


def run_responsivity_lgbm(
    ens,
    scaler,
    valid: Split,
    n_cases: int = config.RESP_N_CASES,
    seed: int = config.SEED,
    deltas: tuple[float, ...] = config.RESP_DELTAS_K,
    horizon: int = config.RESP_HORIZON_H,
    targets: tuple[str, ...] | None = None,
    batch_size: int = 4096,
) -> tuple[ResponsivityResult, np.ndarray]:
    """LightGBM-Offset-Responsivitäts-Test (analog Keras, Q-101)."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    case_idx = select_cases(valid, n_cases=n_cases, seed=seed, horizon_h=horizon)

    def factory(fixed_b: np.ndarray):
        del fixed_b  # Fixierung geschieht in rollout_matrix (State-Update).

        def _pred(xb: np.ndarray) -> np.ndarray:
            return np.asarray(ens.predict_targets(scaler.transform(xb))).reshape(
                len(xb), len(targets)
            )

        return _pred

    hold_p, step_p = _run_rollouts(
        factory, valid, case_idx, deltas, horizon, targets, batch_size=batch_size
    )
    result = compute_result(hold_p, step_p, deltas, targets)
    return result, case_idx
