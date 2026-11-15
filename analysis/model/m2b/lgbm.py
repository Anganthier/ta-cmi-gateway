"""M2b — LightGBM: ein Booster je (Target, Horizont).

- Analog: ``regression_l1`` auf das Delta ``y(t+h) − y(t)`` (Median-Schätzer,
  passend zur MAE-Metrik).
- Digital: ``cross_entropy`` auf den An-Quotienten (0..1) → Ausgabe 0..1.
- Early-Stopping auf Valid-L1 (max. ``LGBM_NUM_BOOST`` Runden).
- Optional ``monotone_constraints`` (P3, :func:`m2b.config.monotone_spec`).
- ``refit``: Neu-Training auf Train+Valid mit ``best_iter × 1,1`` Runden →
  Deploy-Modell (nutzt die jüngsten Daten 2025; Test 2026 bleibt unberührt).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config
from .data import Dataset


@dataclass
class FitInfo:
    target: str
    h: int
    monotone: bool
    best_iter: int
    n_train: int
    n_valid: int
    refit: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class LgbmL1:
    """Container: ``{(target, h): Booster}`` + Fit-Infos."""

    boosters: dict[tuple[str, int], object] = field(default_factory=dict)
    infos: dict[tuple[str, int], FitInfo] = field(default_factory=dict)

    def predict(self, target: str, h: int, X: np.ndarray, threads: int | None = None) -> np.ndarray:
        """Modell-Ausgabe (Delta bzw. Level) für Zeilen ``X``."""
        b = self.boosters[(target, int(h))]
        kw = {"num_threads": int(threads)} if threads else {}
        Xm = mask_excluded(target, np.asarray(X, dtype=np.float32), list(b.feature_name()))
        return np.asarray(b.predict(Xm, **kw), dtype=np.float64)

    def save(self, out_dir: Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for (t, h), b in self.boosters.items():
            b.save_model(str(out / f"{t}__h{h}.txt"))

    @classmethod
    def load(cls, out_dir: Path, keys: list[tuple[str, int]]) -> LgbmL1:
        import lightgbm as lgb

        out = Path(out_dir)
        return cls(boosters={(t, h): lgb.Booster(model_file=str(out / f"{t}__h{h}.txt")) for t, h in keys})


def mask_excluded(target: str, X: np.ndarray, feature_names: list[str]) -> np.ndarray:
    """Setzt die für ``target`` ausgeblendeten Features auf NaN (Kopie, s. config)."""
    excl = config.excluded_features(target, feature_names)
    if not excl:
        return X
    idx = [feature_names.index(n) for n in excl]
    Xm = np.array(X, dtype=np.float32, copy=True)
    Xm[:, idx] = np.nan
    return Xm


def monotone_vector(target: str, feature_names: list[str]) -> list[int]:
    spec = config.monotone_spec(target)
    return [int(spec.get(n, 0)) for n in feature_names]


def _params(target: str, monotone: bool, y_train: np.ndarray, feature_names: list[str],
            threads: int, seed: int) -> dict:
    """Parameter inkl. Monotonie.

    LightGBM erlaubt ``monotone_constraints`` nicht mit ``regression_l1``
    (Leaf-Renewal). Monotone Analog-Varianten nutzen daher ``huber`` mit
    ``alpha = 1,5 × std(Train-Label)`` (innen quadratisch, außen robust).
    """
    params = config.lgbm_params(config.TARGET_KIND[target], seed=seed, threads=threads)
    if config.CONTROLLER_CAUSAL and target in config.CONTROLLER_TARGETS:
        params["metric"] = "l1"  # Kausal-Modus (config.CONTROLLER_CAUSAL)
    if monotone:
        params["monotone_constraints"] = monotone_vector(target, feature_names)
        params["monotone_constraints_method"] = "advanced"
        if params["objective"] == "regression_l1":
            sd = float(np.nanstd(y_train)) if len(y_train) else 1.0
            params["objective"] = "huber"
            params["alpha"] = max(1e-3, 1.5 * sd)
    return params


def fit_one(
    ds: Dataset,
    target: str,
    h: int,
    monotone: bool = False,
    num_boost: int = config.LGBM_NUM_BOOST,
    early_stop: int = config.LGBM_EARLY_STOP,
    threads: int = config.LGBM_THREADS_DEFAULT,
    seed: int = config.SEED,
):
    """Trainiert einen Booster (Train, Early-Stopping auf Valid)."""
    import lightgbm as lgb

    spec = ds.target(target, h)
    tr = ds.rows("train") & spec.mask
    va = ds.rows("valid") & spec.mask
    if tr.sum() < 50 or va.sum() < 10:
        raise ValueError(f"zu wenige Zeilen für {target}@{h}h: train={tr.sum()} valid={va.sum()}")
    params = _params(target, monotone, spec.y[tr], ds.feature_names, threads, seed)
    Xtr = mask_excluded(target, ds.X[tr], ds.feature_names)
    Xva = mask_excluded(target, ds.X[va], ds.feature_names)
    d_tr = lgb.Dataset(Xtr, label=spec.y[tr], feature_name=ds.feature_names, free_raw_data=True)
    d_va = lgb.Dataset(Xva, label=spec.y[va], reference=d_tr)
    b = lgb.train(
        params,
        d_tr,
        num_boost_round=num_boost,
        valid_sets=[d_va],
        valid_names=["valid"],
        callbacks=[lgb.early_stopping(early_stop, verbose=False), lgb.log_evaluation(0)],
    )
    info = FitInfo(target, h, monotone, int(b.best_iteration or num_boost), int(tr.sum()), int(va.sum()))
    return b, info


def refit_full(
    ds: Dataset,
    target: str,
    h: int,
    best_iter: int,
    monotone: bool = False,
    threads: int = config.LGBM_THREADS_DEFAULT,
    seed: int = config.SEED,
):
    """Deploy-Refit auf Train+Valid mit ``max(50, 1,1 × best_iter)`` Runden."""
    import lightgbm as lgb

    spec = ds.target(target, h)
    rows = (ds.rows("train") | ds.rows("valid")) & spec.mask
    params = _params(target, monotone, spec.y[rows], ds.feature_names, threads, seed)
    n_rounds = max(50, int(round(1.1 * best_iter)))
    Xr = mask_excluded(target, ds.X[rows], ds.feature_names)
    d = lgb.Dataset(Xr, label=spec.y[rows], feature_name=ds.feature_names)
    b = lgb.train(params, d, num_boost_round=n_rounds, callbacks=[lgb.log_evaluation(0)])
    info = FitInfo(target, h, monotone, n_rounds, int(rows.sum()), 0, refit=True)
    return b, info


def feature_importance(booster, feature_names: list[str], top: int = 10) -> list[tuple[str, float]]:
    """Top-``top`` Features nach Gain-Anteil."""
    g = np.asarray(booster.feature_importance("gain"), dtype=np.float64)
    tot = g.sum() or 1.0
    idx = np.argsort(g)[::-1][:top]
    return [(feature_names[i], round(float(g[i] / tot), 4)) for i in idx]


def importance_share(
    booster, feature_names: list[str], subset: list[str], top: int = 10
) -> tuple[float, list[tuple[str, float]]]:
    """Gain-Anteil einer Feature-Teilmenge (gesamt) + deren Top-``top`` (Anteil am Gesamt-Gain)."""
    g = np.asarray(booster.feature_importance("gain"), dtype=np.float64)
    tot = g.sum() or 1.0
    sub = set(subset)
    idx = [i for i, n in enumerate(feature_names) if n in sub]
    share = round(float(g[idx].sum() / tot), 4) if idx else 0.0
    best = sorted(idx, key=lambda i: -g[i])[:top]
    return share, [(feature_names[i], round(float(g[i] / tot), 4)) for i in best]
