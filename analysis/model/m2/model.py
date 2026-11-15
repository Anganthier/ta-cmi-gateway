"""M2 — Keras-3-L1: geteilter Stamm + einer Head pro Target (1-h-Step).

Architektur (Design v0.3):
- Input: dichte Feature-Vektoren (State + Lags + Aktion + Wetter-24h +
  Kalender + Flags + NULL-Flag-Spalten), normalisiert (StandardScaler,
  Train-Fit, s. :mod:`m2.normalize`).
- Geteilter Stamm: Dense 256 → 128 → 64 (ReLU, Dropout 0.1).
- Einer Head pro Target: Dense 32 (ReLU) → Dense 1.
  - Analog-Targets: linear (°C / h/h / K).
  - Digital-Targets: **sigmoid** (An-Quotient ∈ [0,1]).
- **Loss: gewichtetes MAE je Target** (Design: „MAE je Target, gewichtet“;
  ``config.LOSS_WEIGHTS``): RAS ist/perc 1,0 · offset 0,8 · hz/buff 0,7 ·
  heat_min 0,5 · Rest 0,4. Digital-Head: MAE auf Sigmoid-Wahrscheinlichkeit
  (Initial-Design, dokumentiert in REPORT).
- ≤ 500k Parameter (Design-Vorgabe; :func:`assert_param_budget`).

Determinismus:
- ``keras.utils.set_random_seed(20261031)`` vor Konstruktion/Training.
- TF-Flags: ``TF_CUBLAS_DETERMINISTIC=1``, ``TF_ENABLE_ONEDNN_OPTS=0``
  (einheitliche Kern-Auswahl) — setzen **vor** dem TF-Import.
  ``TF_CUBLAS_DETERMINISTIC=1`` deckt auch die GPU-Gemms ab (deterministischer
  cuBLAS-Kern); ``device=cpu`` bleibt der Default-Referenzpfad (bit-reproduzierbar).
- Early-Stopping (Patience ≥ 30, ``restore_best_weights=True``) +
  ReduceLROnPlateau (LR 3e-4 Start; Empirie: 1e-3 oszilliert bei
  Multi-Head-MAE).

GPU (opt-in, ``m2.build`` main(): Self-Re-Exec mit ``LD_LIBRARY_PATH`` auf die
nvidia-pip-Bundles; :func:`configure_devices`): Memory-Growth pro GPU (8-GB-
Karte ist geteilt mit dem Desktop → niemals Voll-Pre-Allocation).
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import numpy as np

from . import config


def _apply_tf_determinism() -> None:
    """Setzt TF-Determinismus-Flags (muss vor dem TF-Import geschehen)."""
    os.environ.setdefault("TF_CUBLAS_DETERMINISTIC", "1")
    os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")


_apply_tf_determinism()

import keras  # noqa: E402


def set_determinism(seed: int = config.SEED) -> None:
    """Keras-Seed (NumPy + Python + TF-Global) setzen. Idempotent."""
    keras.utils.set_random_seed(seed)


def configure_devices() -> list[str]:
    """Listet die physischen TF-GPUs, aktiviert Memory-Growth und gibt die
    Device-Namen zurück (z. B. ``["GPU:0"]``) — ``[]`` = CPU-fallback.

    Memory-Growth ist Pflicht: die 8-GB-Quadro ist geteilt mit dem Desktop
    (cosmic-comp) → TF darf nie den gesamten VRAM pre-allozieren.

    Hinweis: diese Funktion muss **vor** der ersten GPU-Workload-Nutzung
    (``model.fit``/``model.predict``) aufgerufen werden; sie ist
    idempotent (zweiter Aufruf liefert dieselbe Liste).
    """
    import tensorflow as tf

    gpus = tf.config.list_physical_devices("GPU")
    devices: list[str] = []
    for i, gpu in enumerate(gpus):
        with contextlib.suppress(RuntimeError, ValueError):
            # Device bereits konfiguriert (idempoter Aufruf) → ignorieren.
            tf.config.experimental.set_memory_growth(gpu, True)
        devices.append(f"GPU:{i}")
    return devices


DIGITAL_SET = frozenset(config.DIGITAL_TARGETS)


def head_activation(target: str) -> str:
    """Head-Aktivierung: Digital → sigmoid, Analog → linear."""
    return "sigmoid" if target in DIGITAL_SET else "linear"


@keras.saving.register_keras_serializable()
class _WeightedMae(keras.losses.MeanAbsoluteError):
    """Pro-Head-MAE mit fester Gewichtung (Design v0.3: „MAE je Target,
    gewichtet“): RAS ist/perc 1,0 · offset 0,8 · hz/buff 0,7 · heat_min 0,5 ·
    Rest 0,4. Ohne Gewichtung würden die grossen analogen Heads (z. B.
    Abgastemperatur) das RAS-Ist-Signal im Shared-Stamm überwuchern.

    Subclass von ``MeanAbsoluteError`` (statt Closure) → sauber
    Serialisierbar (Keras 3 Save/Load des Artefakts) via ``get_config``.
    """

    def __init__(self, weight: float = 1.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.weight = float(weight)

    def call(self, y_true, y_pred):  # noqa: N802 - Keras-Signatur
        return super().call(y_true, y_pred) * self.weight

    def get_config(self) -> dict:
        cfg = super().get_config()
        cfg["weight"] = self.weight
        return cfg


def _weighted_mae_loss(weight: float):
    """Konstruiert ein :class:`_WeightedMae` (pro-Head-MAE × Gewichtung)."""
    return _WeightedMae(weight=float(weight))


def build_model(
    n_features: int,
    targets: tuple[str, ...] | list[str] | None = None,
    hidden: tuple[int, ...] | None = None,
    dropout: float = config.DROPOUT,
    head_hidden: int = config.HEAD_HIDDEN,
    learning_rate: float = config.LEARNING_RATE,
    loss_weights: dict[str, float] | None = None,
    name: str = "cmi_l1",
) -> keras.Model:
    """Konstruiert + kompiliert das L1-Modell (Multi-Output, MAE je Head,
    pro-Target-Gewichtung nach ``config.LOSS_WEIGHTS``)."""
    set_determinism()
    targets = tuple(targets) if targets is not None else config.TARGETS
    hidden = tuple(hidden) if hidden is not None else config.HIDDEN
    loss_weights = loss_weights if loss_weights is not None else config.LOSS_WEIGHTS

    inp = keras.Input(shape=(n_features,), name="features", dtype="float32")
    x = inp
    for li, units in enumerate(hidden):
        x = keras.layers.Dense(units, activation="relu", name=f"stem_{li}_{units}")(x)
        if dropout > 0.0:
            x = keras.layers.Dropout(dropout, name=f"drop_{li}")(x)
    outs: list[keras.layers.Layer] = []
    for t in targets:
        h = keras.layers.Dense(head_hidden, activation="relu", name=f"head_{t}_h")(x)
        outs.append(
            keras.layers.Dense(1, activation=head_activation(t), name=f"head_{t}")(h)
        )
    model = keras.Model(inputs=inp, outputs=outs, name=name)
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=learning_rate),
        loss={
            f"head_{t}": _weighted_mae_loss(float(loss_weights.get(t, 1.0)))
            for t in targets
        },
        # DoD-MAE (gewichtet) wird extern gemessen (m2.model.evaluate_weighted_mae
        # / m2.baseline) — das (gewichtete) val_loss dient Early-Stoppping-/
        # LR-Scheduling-Monitor.
        metrics=None,
    )
    return model


def count_params(model: keras.Model) -> int:
    """Anzahl trainierbarer Parameter."""
    n = 0
    for v in model.weights:
        shape = getattr(v, "shape", None)
        if shape is None:
            continue
        try:
            n += int(np.prod(shape))
        except TypeError:
            continue
    return n


def assert_param_budget(model: keras.Model, max_params: int = config.MAX_PARAMS) -> int:
    """Wärft, wenn die Parameter-Vorgabe (≤ 500k) verletzt wird."""
    n = count_params(model)
    if n > max_params:
        raise AssertionError(f"Parameter-Budget verletzt: {n} > {max_params}")
    return n


def weighted_mae(
    Y_true: np.ndarray,
    Y_pred: np.ndarray,
    targets: tuple[str, ...] | list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """MAE je Target + gewichtetes Gesamt-MAE (REPORT-Metrik)."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    weights = weights or config.LOSS_WEIGHTS
    Y_true = np.asarray(Y_true, dtype=np.float64)
    Y_pred = np.asarray(Y_pred, dtype=np.float64)
    out: dict[str, float] = {}
    total_w = 0.0
    total = 0.0
    for i, t in enumerate(targets):
        w = float(weights[t])
        mae = float(np.mean(np.abs(Y_true[:, i] - Y_pred[:, i])))
        out[t] = mae
        total += w * mae
        total_w += w
    out["weighted_mean"] = float(total / total_w) if total_w else 0.0
    return out


def evaluate_weighted_mae(
    model: keras.Model,
    X: np.ndarray,
    Y: np.ndarray,
    targets: tuple[str, ...] | list[str] | None = None,
    batch_size: int = config.BATCH_SIZE,
) -> dict[str, float]:
    """Modell-MAE je Target (ungewichtet) + gewichtetes MAE."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    pred = predict_targets(model, X, targets=targets, batch_size=batch_size)
    return weighted_mae(Y, pred, targets)


def predict_targets(
    model: keras.Model,
    X: np.ndarray,
    targets: tuple[str, ...] | list[str] | None = None,
    batch_size: int = config.BATCH_SIZE,
) -> np.ndarray:
    """Prädikate aller Heads → (n, n_targets) (TARGETS-Reihenfolge).

    Keras 3 liefert bei Multi-Output-Modellen eine **Liste** von
    (n, 1)-Arrays (Output-Reihenfolge = ``targets``); hier zu
    (n, n_targets) gestapelt.
    """
    del targets
    out = model.predict(X, batch_size=batch_size, verbose=0)
    if isinstance(out, (list, tuple)):
        # Keras 3: Multi-Output → Liste von (n, 1)-Arrays (Output-Reihenfolge
        # = targets). Je Array auf (n,) squashen, dann auf Achse 1 stapeln.
        cols = [np.asarray(o, dtype=np.float64).reshape(len(X)) for o in out]
        return np.stack(cols, axis=1)
    return np.asarray(out, dtype=np.float64).reshape(len(X), -1)


def model_architecture_summary(model: keras.Model | None = None) -> dict[str, object]:
    """Zusammenfassung für metadata/Report (Parameter, Heads, Aktivierung)."""
    n_params = count_params(model) if model is not None else _theoretical_param_count()
    return {
        "n_params": int(n_params),
        "hidden": list(config.HIDDEN),
        "head_hidden": config.HEAD_HIDDEN,
        "dropout": config.DROPOUT,
        "n_heads": len(config.TARGETS),
        "heads": {t: head_activation(t) for t in config.TARGETS},
    }


def _theoretical_param_count(
    n_features: int = config.N_FEATURES,
    targets: tuple[str, ...] | None = None,
    hidden: tuple[int, ...] | None = None,
    head_hidden: int = config.HEAD_HIDDEN,
) -> int:
    """Parameter-Count aus den Abmessungen (ohne Modell zu bauen)."""
    targets = tuple(targets) if targets is not None else config.TARGETS
    hidden = tuple(hidden) if hidden is not None else config.HIDDEN
    n = 0
    prev = n_features
    for u in hidden:
        n += prev * u + u
        prev = u
    for _ in targets:
        n += prev * head_hidden + head_hidden
        n += head_hidden * 1 + 1
    return n


def save_model(model: keras.Model, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(p))


def train_l1(
    model: keras.Model,
    X_train: np.ndarray,
    Y_train: np.ndarray,
    X_valid: np.ndarray,
    Y_valid: np.ndarray,
    targets: tuple[str, ...] | list[str] | None = None,
    epochs: int = config.EPOCHS_DEFAULT,
    patience: int = config.PATIENCE,
    batch_size: int = config.BATCH_SIZE,
) -> tuple[dict[str, object], list[dict[str, float]]]:
    """Trainiert mit Early-Stopping auf (gewichteter) Val-Loss (best-epoch-Weights).

    Der übergebene ``model`` muss bereits mit dem (gewichteten) Multi-Head-MAE
    kompiliert sein (s. :func:`build_model`). DoD-MAE (gewichtet) wird nach dem
    Training separat gemessen (:func:`evaluate_weighted_mae`).

    Returns
    -------
    (history, best_metrics): ``history`` = Keras-Trainings-History
    (``loss``/``val_loss`` je Epoch); ``best_metrics`` = gewichtetes MAE auf
    Valid **mit den best-epoch-Weights** (EarlyStopping lädt sie zurück).
    """
    targets = tuple(targets) if targets is not None else config.TARGETS
    y_train = {f"head_{t}": Y_train[:, i] for i, t in enumerate(targets)}
    y_valid = {f"head_{t}": Y_valid[:, i] for i, t in enumerate(targets)}
    es = keras.callbacks.EarlyStopping(
        monitor="val_loss",
        mode="min",
        patience=patience,
        restore_best_weights=True,
        verbose=0,
    )
    hist = model.fit(
        X_train,
        y_train,
        validation_data=(X_valid, y_valid),
        epochs=epochs,
        batch_size=batch_size,
        callbacks=[es],
        verbose=0,
    )
    best_metrics = evaluate_weighted_mae(model, X_valid, Y_valid, targets)
    return {"history": hist.history}, best_metrics
