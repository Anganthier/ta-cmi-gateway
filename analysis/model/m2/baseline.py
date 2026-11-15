"""M2 — Baselines + DoD-Metriken (B2 Naive, 4-h-sequenzielles Rollout).

**Semantik-Grundlage** (aus M1-SCHEMA, Raster lückenlos): Im Split-Array
(gehaltene Rows, ``gap_full = 0``) gilt für Transition-Index ``i``:
- Input-Row-Zeit ``t_i = split.ts[i]``,
- Target = Wert der Zeile ``i+1`` (nächste Kalenderstunde) = ``split.Y[i]``,
- Wert der aktuellen Stunde ``t_i`` = Target der vorherigen Transition
  ``split.Y[i−1]`` (Persistenz),
- „gleiche Zeit gestern" = Wert von ``t_{i+1} − 24 h`` → Zeilen-Lookup via
  ``ts → Index``-Map (Index-Abstand 24 ist wegen gap-Drops im Array NICHT
  garantierbar; ts-Lookup ist DST-sicher).

**B2 Naive** (fest, nicht getunt, Design §5): pro Target
``0,5 · Persistenz(t) + 0,5 · gleiche Zeit gestern (t−24 h)``.

**B2-4-h-Rollout** (analog zum Keras-Rollout, dokumentiert):
Step 0 wie oben; Step k+1 = ``0,5·Prädikt_k + 0,5·gleiche Zeit gestern``
(gestern-Wert bleibt konstant bei der Ausgangs-Row — Persistenz des letzten
Prädikats + konstanter gestrig-Kontext; konservativ für B2, da kein
Modellfehler-Kumulieren).

DoD-Metriken (auf Valid 2025, 1-h-Modell → 4-h-Rollout):
- MAE +4 h je Target (RAS-Ist je Zone, Puffer, übrige): Keras vs B2 vs LGBM.
- Offset-Responsivität (s. :mod:`m2.responsivity`).
"""
from __future__ import annotations

import numpy as np

from . import config
from .normalize import StandardScaler
from .transitions import Split


def _ts_to_index(split: Split) -> dict:
    """ts → Index-Map im Split-Array (Raster lückenlos → injektiv)."""
    return {ts: i for i, ts in enumerate(split.ts)}


def b2_step(
    y_persist: np.ndarray,
    y_yesterday: np.ndarray,
) -> np.ndarray:
    """B2-1-Step: ``0,5·Persistenz + 0,5·gleiche Zeit gestern`` (pro Target).

    Parameters
    ----------
    y_persist:
        Wert der aktuellen Stunde (Shape (n_targets,)).
    y_yesterday:
        Wert von t−24 h vor der **Ziel**-Stunde (Shape (n_targets,)).
    """
    y_persist = np.asarray(y_persist, dtype=np.float64)
    y_yesterday = np.asarray(y_yesterday, dtype=np.float64)
    if y_persist.shape != y_yesterday.shape:
        raise ValueError("Persistenz- und Yesterday-Shape müssen identisch sein")
    return 0.5 * y_persist + 0.5 * y_yesterday


def b2_4h_series(
    split: Split,
    start_indices: np.ndarray,
    steps: int = config.ROLLOUT_H,
) -> tuple[np.ndarray, int]:
    """B2-4-h-Rollout über alle Start-Indizes.

    Semantik (s. Modul-Doku): für Transition-Index ``i`` (Input-Row t_i,
    Target = Wert von t_{i+1}):
    - Persistenz = Wert der aktuellen Stunde t_i = ``Y[i−1]`` (Fehler:
      ``Y[i]`` wäre der Wert von t_{i+1} = der True-Value → Leak).
    - gleiche Zeit gestern = Wert von t_{i+1} − 24 h → Zeile ``j`` mit
      ``ts[j] = t_{i+1} − 24 h`` → Wert = ``Y[j−1]`` (denn ``Y[j]`` = Wert
      von t_{j+1} = t_{i+2} ≠ t_{i+1} − 24 h; nur ``Y[j−1]`` = Wert von t_j
      = t_{i+1} − 24 h). DST-sicher via ts-Lookup (Index-Abstand 24 ist
      wegen gap-Drops im Array NICHT garantierbar).

    Returns
    -------
    (pred, n_excluded): ``pred`` = (n_starts, steps, n_targets);
    ``n_excluded`` = Anzahl Starts, deren „gestern"-Row nicht im
    Split-Array existiert (gap-Drop → NaN-Zeile, aus Metriken
    ausgeschlossen).
    """
    from datetime import timedelta

    Y = split.Y
    ts_map = _ts_to_index(split)
    out = np.full((len(start_indices), steps, Y.shape[1]), np.nan, dtype=np.float64)
    n_excluded = 0
    for r, i in enumerate(start_indices):
        i = int(i)
        ts_target = split.ts[i] + timedelta(hours=1)  # t_{i+1} (Ziel-Zeit)
        # Persistenz: Wert von t_i (aktuelle Stunde) = Y[i−1], Fallback Y[0].
        persist = Y[i - 1] if i >= 1 else Y[0]
        # Gestern: Wert von ts_target − 24 h = Y[j−1], j = Index von ts_target−24h.
        j = ts_map.get(ts_target - timedelta(hours=24))
        if j is None or j == 0:
            n_excluded += 1
            continue
        yest = Y[j - 1]
        prev = b2_step(persist, yest)
        out[r, 0] = prev
        for k in range(1, steps):
            prev = b2_step(prev, yest)
            out[r, k] = prev
    return out, n_excluded


def mae(pred: np.ndarray, truth: np.ndarray) -> float:
    """MAE (flach über alle Elemente; NaNs ausgeschlossen)."""
    pred = np.asarray(pred, dtype=np.float64)
    truth = np.asarray(truth, dtype=np.float64)
    mask = ~(np.isnan(pred) | np.isnan(truth))
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs(pred[mask] - truth[mask])))


def mae_per_target(pred: np.ndarray, truth: np.ndarray) -> list[float]:
    """MAE je Target-Spalte ((n, n_targets) → (n_targets,))."""
    pred = np.asarray(pred, dtype=np.float64)
    truth = np.asarray(truth, dtype=np.float64)
    return [mae(pred[:, i], truth[:, i]) for i in range(pred.shape[1])]


def mae_rollout(pred: np.ndarray, truth: np.ndarray) -> dict[str, float]:
    """MAE je Rollout-Step + Gesamt (4-h-Metrik-Basis).

    ``pred``/``truth`` = (n_starts, steps, n_targets).
    """
    pred = np.asarray(pred, dtype=np.float64)
    truth = np.asarray(truth, dtype=np.float64)
    out: dict[str, float] = {}
    for k in range(pred.shape[1]):
        out[f"step{k + 1}"] = mae(pred[:, k], truth[:, k])
    out["all"] = mae(pred, truth)
    return out


def rollout_truth(split: Split, start_indices: np.ndarray, steps: int = config.ROLLOUT_H) -> np.ndarray:
    """Tatsächliche Targets für ein Rollout (aus dem Raster).

    Start-Index ``i`` (Input-Row t_i) → k-Prädikt (k=0…steps−1) bezieht
    sich auf den Wert von ``t_{i+1+k}`` = ``Y[i+k]`` (denn ``Y[j]`` = Wert
    von ``t_{j+1}``; für ``j = i+k``: Wert = t_{i+k+1} = t_{i+1+k}).
    Gültig solange ``i+k`` im Split-Array liegt (s.
    :func:`rollout_start_indices`).

    Returns
    -------
    (n_starts, steps, n_targets).
    """
    Y = split.Y
    out = np.empty((len(start_indices), steps, Y.shape[1]), dtype=np.float64)
    for r, i in enumerate(start_indices):
        i = int(i)
        for k in range(steps):
            j = i + k  # Wert von t_{i+1+k} = Y[i+k]
            out[r, k] = Y[j] if 0 <= j < len(Y) else np.nan
    return out


def rollout_start_indices(split: Split, steps: int = config.ROLLOUT_H) -> np.ndarray:
    """Gültige Rollout-Start-Indizes: Input-Row + ``steps`` Ziel-Rows müssen
    im selben Split-Raster liegen (keine Split-Grenzen-Überschneidung).

    Start-Index ``i`` nutzt Truth ``Y[i … i+steps−1]`` (rollout_truth); der
    letzte gültige Start ist ``i = n − steps`` (denn ``i + steps − 1 = n − 1``
    bleibt im Raster) → Indizes ``0 … n − steps`` (``n − steps + 1`` Starts)."""
    n = len(split.ts)
    return np.arange(0, max(0, n - steps + 1), dtype=np.int64)


def _scaled_names(n_features: int) -> list[str]:
    """Feature-Namen (Input + NULL-Flags), für die fixed-Matrix-Bau."""
    from .rollout import feature_names

    return feature_names(n_features)


def rollout_series_keras(
    model,
    scaler: StandardScaler,
    split: Split,
    start_indices: np.ndarray,
    steps: int = config.ROLLOUT_H,
    targets: tuple[str, ...] | list[str] | None = None,
    action_fixed: dict[str, float] | None = None,
    batch_size: int = 2048,
) -> np.ndarray:
    """Keras-Rollout über alle Start-Indizes (batched Matrix-Rollout) →
    (n_starts, steps, n_targets).

    Der Rollout-State-Update erfolgt auf der **Raw-Feature-Ebene**
    (Prädikate sind in Target-Einheiten; nur für die Prädikation wird
    skaliert). s. :mod:`m2.rollout` (Dokumentation der Annahmen).
    """
    from .model import predict_targets
    from .rollout import rollout_matrix
    from .transitions import extended_raw

    targets = tuple(targets) if targets is not None else config.TARGETS
    idx = np.asarray(start_indices, dtype=np.int64)
    X = extended_raw(split)[idx]
    n_features = int(X.shape[1])
    out = np.empty((len(idx), steps, len(targets)), dtype=np.float64)
    for lo in range(0, len(idx), batch_size):
        hi = min(lo + batch_size, len(idx))
        Xb = X[lo:hi]
        if action_fixed is None:
            fixed = None
        else:
            fixed = np.full((len(Xb), n_features), np.nan, dtype=np.float64)
            names = _scaled_names(n_features)
            for n, v in action_fixed.items():
                if n in names:
                    fixed[:, names.index(n)] = float(v)

        def _pred(xb: np.ndarray) -> np.ndarray:
            return predict_targets(model, scaler.transform(xb), batch_size=len(xb))

        out[lo:hi] = rollout_matrix(Xb, _pred, steps=steps, fixed_mat=fixed, targets=targets)
    return out


def rollout_series_lgbm(
    ens,
    scaler: StandardScaler,
    split: Split,
    start_indices: np.ndarray,
    steps: int = config.ROLLOUT_H,
    targets: tuple[str, ...] | list[str] | None = None,
    action_fixed: dict[str, float] | None = None,
    batch_size: int = 4096,
) -> np.ndarray:
    """LightGBM-Rollout (identische Rollout-Logik wie Keras, s.
    :mod:`m2.rollout`) → (n_starts, steps, n_targets).

    Hinweis: LGBM prädiziert intern auf **skaliertem** Input (gleicher
    Scaler wie Keras — für Shared-Pipeline-Äquivalenz; GBDT-Prädikation ist
    unter monotoner Skalierung invariant). Der State-Update erfolgt auf der
    Raw-Ebene (wie Keras).
    """
    from .rollout import rollout_matrix
    from .transitions import extended_raw

    targets = tuple(targets) if targets is not None else config.TARGETS
    idx = np.asarray(start_indices, dtype=np.int64)
    X = extended_raw(split)[idx]
    n_features = int(X.shape[1])
    out = np.empty((len(idx), steps, len(targets)), dtype=np.float64)
    for lo in range(0, len(idx), batch_size):
        hi = min(lo + batch_size, len(idx))
        Xb = X[lo:hi]
        if action_fixed is None:
            fixed = None
        else:
            fixed = np.full((len(Xb), n_features), np.nan, dtype=np.float64)
            names = _scaled_names(n_features)
            for n, v in action_fixed.items():
                if n in names:
                    fixed[:, names.index(n)] = float(v)

        def _pred(xb: np.ndarray) -> np.ndarray:
            return np.asarray(ens.predict_targets(scaler.transform(xb))).reshape(
                len(xb), len(targets)
            )

        out[lo:hi] = rollout_matrix(Xb, _pred, steps=steps, fixed_mat=fixed, targets=targets)
    return out
