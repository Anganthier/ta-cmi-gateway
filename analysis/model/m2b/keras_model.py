"""M2b — Keras v2 (Referenz-Modell, P4).

Behebt die M2-Keras-Ursachen (CIR-Log, Diagnose 2026-10-31):

- **Inputs:** NaN → Train-Median + NULL-Indikator **nur** für Spalten, die im
  Train tatsächlich NaN enthalten (keine im Train konstanten Flags mehr);
  z-Score mit dem reparierten :class:`m2.normalize.StandardScaler` (konstante
  Spalten nur zentriert, Kappung ±10).
- **Targets:** je (Target, Horizont) eine Ausgabe; Analog = Delta, Digital =
  Level; **jede Ausgabe z-standardisiert** (Train-Statistik) → kein Kanal
  dominiert den gemeinsamen Stamm (M2: unskalierte Kelvin, Abgas-std 38 K).
- **Loss:** maskierter Huber (NaN-Targets zählen nicht; RAS vor Einbau /
  eingefroren, Lücken) statt imputierter Targets.
- Läuft auf CPU (``CUDA_VISIBLE_DEVICES=-1``; M2-GPU zeigte keinen Gewinn),
  Seed + Op-Determinismus gesetzt.
"""
from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from analysis.model.m2.normalize import StandardScaler

from . import config
from .data import Dataset


def _keras(seed: int):
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import keras
    import tensorflow as tf

    keras.utils.set_random_seed(int(seed))
    with contextlib.suppress(Exception):  # ältere TF ohne Op-Determinismus
        tf.config.experimental.enable_op_determinism()
    return keras, tf


@dataclass
class KerasPrep:
    """Input-/Output-Vorverarbeitung (JSON-serialisierbar)."""

    medians: np.ndarray
    flag_idx: np.ndarray  # Spaltenindizes mit NULL-Indikator
    scaler: StandardScaler
    out_keys: list[tuple[str, int]]
    out_mean: np.ndarray
    out_std: np.ndarray

    def inputs(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        nan = np.isnan(X)
        Xf = np.where(nan, self.medians[None, :], X)
        flags = nan[:, self.flag_idx].astype(np.float64)
        return self.scaler.transform(np.concatenate([Xf, flags], axis=1))

    def to_dict(self) -> dict:
        return {
            "medians": [float(v) for v in self.medians],
            "flag_idx": [int(i) for i in self.flag_idx],
            "scaler": self.scaler.to_dict(),
            "out_keys": [[t, int(h)] for t, h in self.out_keys],
            "out_mean": [float(v) for v in self.out_mean],
            "out_std": [float(v) for v in self.out_std],
        }

    @classmethod
    def from_dict(cls, d: dict) -> KerasPrep:
        return cls(
            medians=np.asarray(d["medians"], dtype=np.float64),
            flag_idx=np.asarray(d["flag_idx"], dtype=np.int64),
            scaler=StandardScaler.from_dict(d["scaler"]),
            out_keys=[(str(t), int(h)) for t, h in d["out_keys"]],
            out_mean=np.asarray(d["out_mean"], dtype=np.float64),
            out_std=np.asarray(d["out_std"], dtype=np.float64),
        )


def fit_prep(ds: Dataset, keys: list[tuple[str, int]]) -> tuple[KerasPrep, np.ndarray]:
    """Fit der Vorverarbeitung auf Train; liefert auch die Label-Matrix (n, K)."""
    tr = ds.rows("train")
    Xtr = ds.X[tr].astype(np.float64)
    with np.errstate(all="ignore"):
        med = np.nanmedian(np.where(np.isfinite(Xtr), Xtr, np.nan), axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    nan_tr = np.isnan(Xtr)
    frac = nan_tr.mean(axis=0)
    flag_idx = np.flatnonzero((frac > 0) & (frac < 1))
    Xf = np.where(nan_tr, med[None, :], Xtr)
    scaler = StandardScaler.fit(np.concatenate([Xf, nan_tr[:, flag_idx].astype(np.float64)], axis=1))
    Y = np.stack([ds.target(t, h).y for t, h in keys], axis=1)
    with np.errstate(all="ignore"):
        mu = np.nanmean(Y[tr], axis=0)
        sd = np.nanstd(Y[tr], axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.where(np.isfinite(sd) & (sd > 1e-6), sd, 1.0)
    return KerasPrep(med, flag_idx, scaler, list(keys), mu, sd), Y


def masked_huber(delta: float = config.KERAS_HUBER_DELTA):
    import tensorflow as tf

    def loss(y_true, y_pred):
        mask = tf.math.is_finite(y_true)
        yt = tf.where(mask, y_true, tf.zeros_like(y_true))
        err = tf.abs(yt - y_pred)
        quad = tf.minimum(err, delta)
        lin = err - quad
        el = 0.5 * quad * quad + delta * lin
        m = tf.cast(mask, y_pred.dtype)
        return tf.reduce_sum(el * m) / tf.maximum(tf.reduce_sum(m), 1.0)

    return loss


def build_model(n_in: int, n_out: int, seed: int = config.SEED):
    keras, _ = _keras(seed)
    inp = keras.Input(shape=(n_in,), name="x")
    x = inp
    for i, u in enumerate(config.KERAS_HIDDEN):
        x = keras.layers.Dense(u, activation="relu", name=f"dense_{i}")(x)
        x = keras.layers.Dropout(config.KERAS_DROPOUT, seed=seed + i)(x)
    out = keras.layers.Dense(n_out, name="y")(x)
    return keras.Model(inp, out, name="cmi_l1_v003_keras")


@dataclass
class KerasL1:
    model: object
    prep: KerasPrep
    history: dict | None = None

    def predict_all(self, X: np.ndarray) -> np.ndarray:
        """(n, K) Modell-Ausgaben in Original-Einheit (Delta bzw. Level)."""
        Z = self.model.predict(self.prep.inputs(X), batch_size=4096, verbose=0)
        out = np.asarray(Z, dtype=np.float64) * self.prep.out_std + self.prep.out_mean
        for j, (t, _h) in enumerate(self.prep.out_keys):
            if config.TARGET_KIND[t] == "digital":
                out[:, j] = np.clip(out[:, j], 0.0, 1.0)
        return out

    def predict(self, target: str, h: int, X: np.ndarray) -> np.ndarray:
        j = self.prep.out_keys.index((target, int(h)))
        return self.predict_all(X)[:, j]

    def save(self, out_dir: Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.model.save(out / "model.keras")
        (out / "prep.json").write_text(json.dumps(self.prep.to_dict()))
        if self.history:
            (out / "history.json").write_text(json.dumps(self.history))

    @classmethod
    def load(cls, out_dir: Path) -> KerasL1:
        keras, _ = _keras(config.SEED)
        out = Path(out_dir)
        model = keras.models.load_model(out / "model.keras", compile=False)
        prep = KerasPrep.from_dict(json.loads((out / "prep.json").read_text()))
        return cls(model=model, prep=prep)


def train(
    ds: Dataset,
    keys: list[tuple[str, int]],
    epochs: int = config.KERAS_EPOCHS,
    patience: int = config.KERAS_PATIENCE,
    seed: int = config.SEED,
    verbose: int = 0,
) -> KerasL1:
    """Trainiert Keras v2 (Train, Early-Stopping auf Valid-Loss)."""
    keras, _ = _keras(seed)
    prep, Y = fit_prep(ds, keys)
    Z = (Y - prep.out_mean) / prep.out_std  # NaN bleibt NaN (Maske)
    tr = ds.rows("train") & np.isfinite(Z).any(axis=1)
    va = ds.rows("valid") & np.isfinite(Z).any(axis=1)
    Xtr = prep.inputs(ds.X[tr])
    Xva = prep.inputs(ds.X[va])
    model = build_model(Xtr.shape[1], len(keys), seed=seed)
    model.compile(optimizer=keras.optimizers.Adam(config.KERAS_LR), loss=masked_huber())
    cb = [
        keras.callbacks.EarlyStopping(monitor="val_loss", patience=patience, restore_best_weights=True),
        keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=max(2, patience // 3)),
    ]
    hist = model.fit(
        Xtr, Z[tr].astype(np.float32),
        validation_data=(Xva, Z[va].astype(np.float32)),
        epochs=epochs, batch_size=config.KERAS_BATCH, shuffle=True, verbose=verbose, callbacks=cb,
    )
    h = {k: [float(x) for x in v] for k, v in hist.history.items()}
    return KerasL1(model=model, prep=prep, history=h)
