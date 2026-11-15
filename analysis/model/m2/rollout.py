"""M2 — gemeinsamer Rollout-Kern (1-h-Steps, sequenziell).

Keras- und LightGBM-Rollout teilen dieselbe State-/Kalender-/Wetter-Update-
Logik (s. :mod:`m2.baseline` / :mod:`m2.responsivity`). Der Kern arbeitet auf
**Raw-Feature-Dicts** (``config.FEATURE_INPUTS``-Namen → Float), so dass beide
Modellfamilien den selben Rollout-Pfad durchlaufen.

Annahmen (dokumentiert, REPORT §Probleme/Annahmen):
- **State-Update**: Target-Prädikate ersetzen die State-Spalten (alle
  ``ANALOG_TARGETS`` + ``ext``-Input bleibt konstant).
- **Digital-Actions**: ``action_fixed`` vorhanden → konstant (Hold-/Step-Run,
  Offset-Responsivität); sonst → Prädikat aus dem vorherigen Step
  (natürlicher Rollout), auf [0,1] geklemmt (An-Quotient).
- **Offsets**: ``action_fixed`` vorhanden → konstant (Offset-Halt/-Step);
  sonst → Prädikat aus dem vorherigen Step.
- **Wetter-Fenster**: bleibt konstant (t→t+23-Fenster der Ausgangsstunde;
  kein 1-h-Verschieben im Rollout — am einfachsten, dokumentiert).
- **Kalender**: Stunde +1 (mod 24), Wochentag bei Tageswechsel,
  ``is_dst``/``is_holiday``/Feiertag konstant (Rollout-Horizont < 24 h).
- **Lags**: bleiben beim t0-Wert (keine Fortschreibung; 4–6-h-Horizont,
  dokumentierte Vereinfachung).
- **NULL-Flag-Spalten** (am Ende von X): bleiben beim t0-Wert.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from . import config
from .model import predict_targets
from .normalize import StandardScaler

PRED_FN = Callable[[dict[str, float]], np.ndarray]  # raw-Features → (n_targets,)


def feature_names(n_features: int) -> list[str]:
    """Feature-Namen für eine (n_features)-breite Extended-Matrix:
    ``config.FEATURE_INPUTS`` + NULL-Flags (``_null`` / ``_tgt_null``),
    Reihenfolge s. :func:`m2.transitions.flag_columns`."""
    from .transitions import flag_columns

    names = list(config.FEATURE_INPUTS) + list(flag_columns())
    if len(names) != n_features:
        # Fallback (Mini-Test/Custom): Flags generisch benennen.
        names = list(config.FEATURE_INPUTS) + [
            f"_flag_{i}" for i in range(n_features - len(config.FEATURE_INPUTS))
        ]
    return names[:n_features]


@dataclass
class RolloutConfig:
    """Rollout-Parameter."""

    steps: int = config.ROLLOUT_H
    targets: tuple[str, ...] = config.TARGETS
    # action_fixed: Offset-/Digital-Hold bzw. -Step (Responsivitäts-Test);
    # None → natürliche Fortschreibung aus Prädikaten.
    action_fixed: dict[str, float] | None = None


def raw_from_vector(scaler: StandardScaler, X0_scaled: np.ndarray) -> dict[str, float]:
    """Skalierter Vektor → Raw-Feature-Dict (inverse-Transform)."""
    X0 = np.asarray(X0_scaled, dtype=np.float32).reshape(-1)
    n_features = int(X0.shape[0])
    names = feature_names(n_features)
    raw_arr = scaler.inverse_transform(X0.reshape(1, -1))[0]
    return {n: float(raw_arr[i]) for i, n in enumerate(names)}


def raw_from_split_row(split, i: int) -> dict[str, float]:
    """Split-Zeile (Raw-X) → Feature-Dict (Flags sind in Split.X bereits
    enthalten, falls :func:`m2.transitions.with_null_flags` verwendet wurde)."""
    from .transitions import Split  # noqa: F401 - Typ-Referenz

    names = feature_names(split.X.shape[1])
    row = split.X[i]
    return {n: float(row[k]) for k, n in enumerate(names)}


def _advance_calendar(raw: dict[str, float]) -> None:
    """Stunde +1 (mod 24), Wochentag bei Tageswechsel (in-place)."""
    hour = int(round(raw["hour"]))
    dow = int(round(raw["day_of_week"]))
    hour_next = (hour + 1) % 24
    dow_next = (dow + 1) % 7 if hour_next == 0 else dow
    raw["hour"] = float(hour_next)
    raw["day_of_week"] = float(dow_next)
    raw["is_weekend"] = float(1 if dow_next >= 5 else 0)


def apply_rollout_step(
    raw: dict[str, float],
    pred: np.ndarray,
    targets: tuple[str, ...] | list[str],
    action_fixed: dict[str, float] | None = None,
) -> dict[str, float]:
    """Fortschreibung der Raw-Features um 1 h (in-place; gibt ``raw`` zurück).

    - State: Target-Prädikate → State-Spalten (``ANALOG_TARGETS`` +
      ``solar_coll``-Input; ``ext`` konstant).
    - Digital-Actions: Prädikat (geclippt) oder ``action_fixed`` (Hold).
    - Offsets: ``action_fixed`` oder Prädikat (natürlicher Rollout).
    - Kalender: +1 h.
    """
    pred = np.asarray(pred, dtype=np.float64)
    pred_map = {t: float(pred[i]) for i, t in enumerate(targets)}
    for t in config.ANALOG_TARGETS:
        if t in raw and t in pred_map:
            raw[t] = pred_map[t]
    for n in config.DIGITAL_ACTION_INPUTS:
        if n in raw:
            if action_fixed is not None and n in action_fixed:
                raw[n] = float(action_fixed[n])
            elif n in pred_map:
                raw[n] = float(np.clip(pred_map[n], 0.0, 1.0))
    for n in config.OFFSET_INPUTS:
        if n in raw:
            if action_fixed is not None and n in action_fixed:
                raw[n] = float(action_fixed[n])
            elif n in pred_map:
                raw[n] = pred_map[n]
    _advance_calendar(raw)
    return raw


def rollout(
    raw0: dict[str, float],
    pred_fn: PRED_FN,
    cfg: RolloutConfig,
) -> list[np.ndarray]:
    """Allgemeiner Rollout: ``pred_fn`` erhält Raw-Features → (n_targets,).

    Returns
    -------
    ``cfg.steps`` Prädikat-Arrays (Reihenfolge = Rollout-Schritte).
    """
    targets = tuple(cfg.targets)
    raw = dict(raw0)
    preds: list[np.ndarray] = []
    for step in range(cfg.steps):
        p = np.asarray(pred_fn(raw), dtype=np.float64)
        preds.append(p)
        if step < cfg.steps - 1:
            apply_rollout_step(raw, p, targets, cfg.action_fixed)
    return preds


def rollout_keras(
    model,
    scaler: StandardScaler,
    X0_scaled: np.ndarray,
    steps: int = config.ROLLOUT_H,
    action_fixed: dict[str, float] | None = None,
    targets: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, float]]:
    """Keras-Rollout-Wrapper (s. :func:`m2.model.rollout_predict`-Doku)."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    raw = raw_from_vector(scaler, X0_scaled)

    def _pred(r: dict[str, float]) -> np.ndarray:
        names = list(raw.keys())
        vec = np.asarray([r.get(n, 0.0) for n in names], dtype=np.float32)
        scaled = scaler.transform(vec.reshape(1, -1))[0]
        return predict_targets(model, scaled.reshape(1, -1))[0]

    cfg = RolloutConfig(steps=steps, targets=targets, action_fixed=action_fixed)
    preds = rollout(raw, _pred, cfg)
    return [{t: float(p[i]) for i, t in enumerate(targets)} for p in preds]


def rollout_lgbm(
    predict_fn,
    raw0: dict[str, float],
    steps: int = config.ROLLOUT_H,
    action_fixed: dict[str, float] | None = None,
    targets: tuple[str, ...] | list[str] | None = None,
) -> list[np.ndarray]:
    """LightGBM-Rollout (``predict_fn``: raw-Features → (n_targets,))."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    cfg = RolloutConfig(steps=steps, targets=targets, action_fixed=action_fixed)
    return rollout(dict(raw0), predict_fn, cfg)


# --- Batched Matrix-Rollout (Performance: 8.7k Starts × 4 Steps, Keras/LGBM) ---


def state_feature_indices(n_features: int) -> dict[str, int]:
    """Index-Mapping für alle State-/Action-/Kalender-Spalten (Raw-Feature-
    Position, ``config.FEATURE_INPUTS``-Reihenfolge + Flags am Ende)."""
    idx = {n: i for i, n in enumerate(feature_names(n_features))}
    return idx


def rollout_matrix(
    raw_mat: np.ndarray,
    pred_fn,
    steps: int = config.ROLLOUT_H,
    fixed_mat: np.ndarray | None = None,
    targets: tuple[str, ...] | list[str] | None = None,
) -> np.ndarray:
    """Batched sequenzieller Rollout über eine (n_cases, n_features)-Matrix.

    ``pred_fn`` erhält (n_cases, n_features) float32 → liefert
    (n_cases, n_targets) (TARGETS-Reihenfolge). State-/Kalender-/Action-
    Update-Logik **identisch** zu :func:`apply_rollout_step` (vektoriert).

    Parameters
    ----------
    raw_mat:
        Raw-Feature-Matrix der Ausgangs-Zustände (n_cases, n_features).
    pred_fn:
        Batch-Prädiktor (Keras: ``model.predict(scaler.transform(x))``;
        LGBM: ``ens.predict_targets(x)``).
    steps:
        Rollout-Horizont.
    fixed_mat:
        (n_cases, n_features) mit NaN = „nicht fixiert", Wert = konstanter
        Wert für das Rollout (Offset-Halt/-Step, Digital-Hold). None =
        natürlicher Rollout (Fortschreibung aus Prädikaten).
    targets:
        Target-Reihenfolge (Default ``config.TARGETS``).

    Returns
    -------
    (n_cases, steps, n_targets).
    """
    targets = tuple(targets) if targets is not None else config.TARGETS
    R = np.array(raw_mat, dtype=np.float64, copy=True)
    n_cases, n_features = R.shape
    fixed = (
        np.full((n_cases, n_features), np.nan, dtype=np.float64)
        if fixed_mat is None
        else np.array(fixed_mat, dtype=np.float64)
    )
    idx = {n: i for i, n in enumerate(feature_names(n_features))}
    # State-Update: Target-Prädikat → State-Spalte (nur vorhandene Features).
    state_updates: list[tuple[int, int]] = []
    for t in targets:
        if t in idx:
            state_updates.append((idx[t], targets.index(t)))
    digital_updates: list[tuple[int, int]] = [
        (idx[n], targets.index(n)) for n in config.DIGITAL_ACTION_INPUTS if n in idx
    ]
    offset_updates: list[tuple[int, int]] = [
        (idx[n], targets.index(n)) for n in config.OFFSET_INPUTS if n in idx
    ]
    i_hour, i_dow = idx["hour"], idx["day_of_week"]

    out = np.zeros((n_cases, steps, len(targets)), dtype=np.float64)
    for s in range(steps):
        P = np.asarray(pred_fn(R.astype(np.float32)), dtype=np.float64).reshape(
            n_cases, len(targets)
        )
        out[:, s] = P
        if s == steps - 1:
            break
        for j, p in state_updates:
            R[:, j] = P[:, p]
        for j, p in digital_updates:
            vals = np.where(np.isnan(fixed[:, j]), np.clip(P[:, p], 0.0, 1.0), fixed[:, j])
            R[:, j] = vals
        for j, p in offset_updates:
            R[:, j] = np.where(np.isnan(fixed[:, j]), P[:, p], fixed[:, j])
        hour = R[:, i_hour].astype(np.int64)
        dow = R[:, i_dow].astype(np.int64)
        hour_next = (hour + 1) % 24
        dow_next = np.where(hour_next == 0, (dow + 1) % 7, dow)
        R[:, i_hour] = hour_next.astype(np.float64)
        R[:, i_dow] = dow_next.astype(np.float64)
        R[:, idx["is_weekend"]] = (dow_next >= 5).astype(np.float64)
    return out
