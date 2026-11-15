"""M2 — LightGBM-Pflicht-Vergleich (Q-101).

Gleiches Feature-Set, gleiche Targets, gleiche Split-Logik wie Keras-L1:
pro Target ein deterministisches LightGBM-Regression-Modell
(``objective='regression_l1'`` = MAE, ``deterministic=True``, fester Seed).

- Features = derselbe (skalierter) Vektor wie Keras (gleicher Scaler,
  Train-Fit). Skalierung ändert GBDT-Ergebnis nicht (monotone
  Transformation), wird aber für die **Shared-Pipeline-Äquivalenz**
  (identische Artefakt-Spalten, ``scaler.json``) beibehalten.
- Digital-Targets: MAE auf [0,1]-Wahrscheinlichkeit (analog Keras-Sigmoid,
  ohne Sigmoid-Transformation — GBDT lernt den An-Quotient direkt;
  dokumentiert).
- Rollout: identisch zum Keras-Rollout (s. :mod:`m2.rollout`,
  :func:`m2.rollout.rollout_lgbm`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config


@dataclass
class LGBMEnsemble:
    """Ein LightGBM-Booster je Target (Reihenfolge = ``config.TARGETS``)."""

    models: list
    targets: tuple[str, ...] = config.TARGETS
    params: dict[str, object] | None = None

    def predict_targets(self, X: np.ndarray) -> np.ndarray:
        """Prädikate für alle Targets → (n, n_targets) (TARGETS-Reihenfolge)."""
        X = np.asarray(X, dtype=np.float32)
        preds = [m.predict(X, num_threads=max(1, _n_threads())) for m in self.models]
        return np.stack([np.asarray(p, dtype=np.float64).reshape(-1) for p in preds], axis=1)

    def predict_raw(self, X: np.ndarray) -> np.ndarray:
        """Wie :meth:`predict_targets` (Alias für Rollout-``pred_fn``)."""
        return self.predict_targets(X)

    def save(self, out_dir: str | Path) -> dict[str, Path]:
        """Spart je Target ``{target}.txt`` (LGBM-Format)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths: dict[str, Path] = {}
        for t, m in zip(self.targets, self.models, strict=False):
            p = out / f"{t}.txt"
            m.save_model(str(p))
            paths[t] = p
        return paths

    @classmethod
    def load(cls, out_dir: str | Path, targets: tuple[str, ...] | None = None) -> LGBMEnsemble:
        import lightgbm as lgb

        targets = tuple(targets) if targets is not None else config.TARGETS
        out = Path(out_dir)
        models = [lgb.Booster(model_file=str(out / f"{t}.txt")) for t in targets]
        return cls(models=models, targets=targets)


def _n_threads() -> int:
    import os

    return int(os.environ.get("LGBM_THREADS", "8"))


def lgbm_params(seed: int = config.SEED, device: str = "cpu") -> dict[str, object]:
    """Deterministische LGBM-Parameter (MAE-Objektiv).

    ``device``:
    - ``"cpu"`` (Default): bit-reproduzierbar (``deterministic=True`` +
      ``force_row_wise=True``) → DoD-Reproduktion.
    - ``"gpu"`` (opt-in, ``build --lgbm-gpu``): GPU-Gemms sind **nicht**
      bit-reproduzierbar garantierbar; ``deterministic`` bleibt dennoch
      gesetzt (deterministischer Kern, wo verfügbar) — GPU-Runs liefern
      daher Ergebnisse im Toleranz-Band, nicht exakt gleiche Zahlen.
      ``force_row_wise`` ist CPU-only → auf GPU entfällt.
      Default bleibt CPU (bit-reproduzierbar, DoD-Reproduktion).
    """
    params = {
        "objective": "regression_l1",
        "metric": "l1",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "max_depth": -1,
        "min_child_samples": 20,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "lambda_l1": 0.0,
        "lambda_l2": 1.0,
        "seed": int(seed),
        "device": "gpu" if device == "gpu" else "cpu",
        "deterministic": True,
        "verbosity": -1,
    }
    if device != "gpu":
        params["force_row_wise"] = True
    return params


def train_ensemble(
    X_train: np.ndarray,
    Y_train: np.ndarray,
    X_valid: np.ndarray,
    Y_valid: np.ndarray,
    targets: tuple[str, ...] | list[str] | None = None,
    num_boost_round: int = config.LGBM_NUM_BOOST,
    early_stopping_rounds: int = config.LGBM_EARLY_STOPPING_ROUNDS,
    weights: dict[str, float] | None = None,
    device: str = "cpu",
) -> tuple[LGBMEnsemble, dict[str, object]]:
    """Trainiert je Target einen Booster (Early-Stopping auf Valid-L1).

    Gewichtung: pro-Target-``sample_weight``-Skalierung (``weights``),
    analog zum Keras-Design (Initial-Values aus ``config.LOSS_WEIGHTS``).

    ``device``: ``"cpu"`` (Default, bit-reproduzierbar) oder ``"gpu"``
    (opt-in; nicht bit-reproduzierbar garantierbar, s. :func:`lgbm_params`).
    """
    import lightgbm as lgb

    targets = tuple(targets) if targets is not None else config.TARGETS
    weights = weights or config.LOSS_WEIGHTS
    params = lgbm_params(device=device)
    Xs = np.asarray(X_train, dtype=np.float32)
    Xv = np.asarray(X_valid, dtype=np.float32)

    boosters: list = []
    evals: dict[str, object] = {}
    for i, t in enumerate(targets):
        w = float(weights[t])
        ds_tr = lgb.Dataset(Xs, label=Y_train[:, i], weight=np.full(len(Xs), w, dtype=np.float32))
        ds_va = lgb.Dataset(Xv, label=Y_valid[:, i], reference=ds_tr)
        booster = lgb.train(
            params,
            ds_tr,
            num_boost_round=num_boost_round,
            valid_sets=[ds_va],
            valid_names=["valid"],
            callbacks=[_quiet_log(), _early_stop(early_stopping_rounds)],
        )
        boosters.append(booster)
        evals[t] = {"best_iteration": int(booster.best_iteration), "best_valid_l1": float(booster.best_score["valid"]["l1"])}
    return LGBMEnsemble(models=boosters, targets=targets, params=params), evals


def _early_stop(rounds: int):
    import lightgbm as lgb

    return lgb.early_stopping(stopping_rounds=rounds, first_metric_only=True)


def _quiet_log():
    import lightgbm as lgb

    return lgb.log_evaluation(period=0)


def evaluate(ens: LGBMEnsemble, X: np.ndarray, Y: np.ndarray) -> dict[str, float]:
    """MAE je Target (ungewichtet) — vergleichbar mit Keras-Eval."""
    from .model import weighted_mae

    pred = ens.predict_targets(X)
    return weighted_mae(Y, pred, ens.targets)
