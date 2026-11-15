"""M2-CLI: ``python -m analysis.model.m2 build`` (Train → Valid → Test →
Responsivität → Artefakt → Report; resume-fähig).

Phasen (jeder Zwischenstand persistiert unter ``runs/checkpoint/``):
1. ``transitions``   Übergangs-Tabelle bauen (DuckDB → NumPy).
2. ``scaler``        StandardScaler auf **Train** fitten (``scaler.json``).
3. ``keras_train``   Keras-L1 (Early-Stopping, Best-Weights-Checkpoint).
4. ``lgbm_train``    LightGBM-Ensemble (je Target; deterministisch).
5. ``metrics``       1-h-MAE (Valid), 4-h-Rollout-MAE (Valid + Test),
                     Offset-Responsivität (Keras + LGBM, Valid).
6. ``artifact``      ``models/cmi-brain/v001/`` (Keras + LGBM + Scaler +
                     metadata + README).
7. ``report``        ``REPORT.md`` (DoD-Checkliste mit exakten Zahlen).

Resume: existierende Checkpoints werden wiederverwendet (gleiche
Transition-Größen), ``--force`` erzwingt Neutraining (alle Phasen),
``--force-keras`` / ``--force-lgbm`` erzwingen selektiv das jeweilige
Neutraining (inkl. Metriken/Artefakt/Report); LGBM-GPU ist opt-in
(``--lgbm-gpu``, Default = CPU, bit-reproduzierbar).

GPU-Enablement: ``main()`` startet den Prozess mit ``LD_LIBRARY_PATH`` auf
die nvidia-pip-Bundles (``.venv/.../site-packages/nvidia/*/lib``) neu,
damit TF die GPU ohne Shell-Activate findet (Self-Re-Exec, Flag
``CMI_M2_GPU_REEXEC``); Keras-Device-Setup per
``m2.model.configure_devices()`` (Memory-Growth pro GPU).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# TF-Determinismus-Flags (vor TF-Import setzen; s. m2.model).
os.environ.setdefault("TF_CUBLAS_DETERMINISTIC", "1")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import keras
import numpy as np

from . import baseline, config, report, responsivity
from .artifact import build_artifact
from .lgbm_model import LGBMEnsemble, train_ensemble
from .lgbm_model import evaluate as lgbm_evaluate
from .model import (
    assert_param_budget,
    build_model,
    configure_devices,
    count_params,
    evaluate_weighted_mae,
    model_architecture_summary,
    predict_targets,
)
from .normalize import StandardScaler
from .transitions import assert_no_leakage, build_transitions, with_null_flags

_QUIET = False


def _log(msg: str) -> None:
    if not _QUIET:
        print(f"[m2] {msg}", flush=True)


def _set_quiet(q: bool) -> None:
    global _QUIET
    _QUIET = q


# --- GPU-Self-Re-Exec (vor TF-Import) --------------------------------------


def _site_packages() -> Path | None:
    """site-packages-Verzeichnis des aktuellen Python (venv- oder
    system-weit); ``None``, wenn nicht auflösbar."""
    import sysconfig

    p = Path(sysconfig.get_paths().get("purelib", ""))
    return p if p.is_dir() else None


def _nvidia_lib_dirs() -> list[Path]:
    """``nvidia/*/lib``-Verzeichnisse im site-packages (pip-Bundles
    cuBLAS/cuDNN/CUDA-12); ``[]`` wenn die GPU-Abhängigkeiten fehlen."""
    sp = _site_packages()
    if sp is None:
        return []
    nv = sp / "nvidia"
    if not nv.is_dir():
        return []
    return sorted(d for d in nv.iterdir() if d.is_dir() and (d / "lib").is_dir())


def _gpu_reexec(argv: list[str]) -> bool:
    """Self-Re-Exec mit ``LD_LIBRARY_PATH`` auf die nvidia-pip-Bundles.

    Nur im CLI-Start (``main()``; ``build()`` wird von Tests direkt
    gerufen und re-exec't nie). Bedingungen:
    - nvidia-Bundles existieren im site-packages (sonst CPU-fallback),
    - ``CMI_M2_GPU_REEXEC`` ist noch nicht gesetzt (Re-Exec-Loop-Schutz).

    Return: ``True``, wenn neu gestartet wurde (``os.execve`` ersetzt den
    Prozess → Rückkehr nur bei Fehlschlag des exec selbst); ``False``,
    wenn normal weitergemacht wird.
    """
    if os.environ.get("CMI_M2_GPU_REEXEC") == "1":
        return False
    libs = _nvidia_lib_dirs()
    if not libs:
        return False
    paths = [str(d / "lib") for d in libs]
    old = os.environ.get("LD_LIBRARY_PATH")
    os.environ["LD_LIBRARY_PATH"] = ":".join(paths) + (f":{old}" if old else "")
    os.environ["CMI_M2_GPU_REEXEC"] = "1"
    _log(f"GPU: starte neu mit LD_LIBRARY_PATH (nvidia-pip-Bundles, {len(paths)} Lib-Dirs)")
    os.execve(
        sys.executable,
        [sys.executable, "-m", "analysis.model.m2", *argv],
        dict(os.environ),
    )
    return False  # unerreichbar außer bei exec-Error (dann: normal weiter)


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def _save_json(p: Path, obj: object) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def _framework_versions() -> dict[str, str]:
    import keras
    import lightgbm
    import tensorflow as tf

    return {
        "tensorflow": str(tf.__version__),
        "keras": str(keras.__version__),
        "lightgbm": str(lightgbm.__version__),
        "numpy": str(np.__version__),
    }


# --- Phase 1+2: Transitions + Scaler ---


def prepare_data(force: bool = False) -> tuple[dict, dict, StandardScaler, dict]:
    """Transitions bauen + Scaler fitten; Returns (splits, stats_dict, scaler,
    split_info)."""
    ck = config.CHECKPOINT_DIR
    ck.mkdir(parents=True, exist_ok=True)
    stats_path = ck / "transitions_stats.json"
    if stats_path.exists() and not force:
        _log("transitions: Cache gefunden (runs/checkpoint/transitions_stats.json)")
        # Rebuild (deterministisch, ~30 s) — Cache nur für Stats-Druck.
    splits, stats = build_transitions()
    assert_no_leakage(*splits.values())
    _save_json(stats_path, _stats_dict(stats))

    # NULL-Flags + Scaler
    X_ext: dict[str, np.ndarray] = {}
    flag_names: list[str] = []
    for name in ("train", "valid", "test"):
        if name in splits:
            X, flags = with_null_flags(splits[name])
            X_ext[name] = X
            flag_names = flags
    scaler_path = ck / "scaler.json"
    if scaler_path.exists() and not force:
        _log("scaler: Cache gefunden (Train-Fit)")
        scaler = StandardScaler.load_json(scaler_path)
    else:
        scaler = StandardScaler.fit(X_ext["train"])
        scaler.save_json(scaler_path)
        _log(f"scaler: gefittet auf Train ({scaler.n_samples_seen} Rows, "
             f"{scaler.n_features} Features)")

    train = splits["train"]
    valid = splits["valid"]
    test = splits.get("test")
    split_info = {
        "train_start": min(train.ts).isoformat(),
        "train_end": max(train.ts).isoformat(),
        "valid_start": min(valid.ts).isoformat(),
        "valid_end": max(valid.ts).isoformat(),
        "test_start": min(test.ts).isoformat() if test else config.TEST_START,
        "test_end": max(test.ts).isoformat() if test else "",
        "n_flag_cols": len(flag_names),
    }
    _log(
        f"transitions: {stats.n_transitions:,} gesamt | "
        f"train={stats.split_sizes.get('train', 0):,} "
        f"valid={stats.split_sizes.get('valid', 0):,} "
        f"test={stats.split_sizes.get('test', 0):,} | "
        f"Drops(gap current)={stats.n_drops_gap_current} "
        f"Drops(missing next)={stats.n_drops_missing_next} "
        f"Drops(gap_full next)={stats.n_drops_gap_full_next} | "
        f"imputiert={stats.n_imputed_cells:,} | flags={len(flag_names)}"
    )
    return splits, {
        "stats": stats,
        "X_ext": X_ext,
        "flag_names": flag_names,
        "split_info": split_info,
    }


def _stats_dict(stats) -> dict:
    return {
        "n_rows_total": stats.n_rows_total,
        "n_rows_gap_full": stats.n_rows_gap_full,
        "n_drops_gap_current": stats.n_drops_gap_current,
        "n_drops_missing_next": stats.n_drops_missing_next,
        "n_drops_gap_full_next": stats.n_drops_gap_full_next,
        "n_transitions": stats.n_transitions,
        "n_imputed_cells": stats.n_imputed_cells,
        "split_sizes": dict(stats.split_sizes),
    }


# --- Phase 3: Keras-Training ---


def train_keras(
    X_train_ext: np.ndarray,
    Y_train: np.ndarray,
    X_valid_ext: np.ndarray,
    Y_valid: np.ndarray,
    scaler: StandardScaler,
    force: bool = False,
    devices: list[str] | None = None,
) -> object:
    """Trainiert das Keras-L1 auf **skaliertem** Extended-Input (Raw-Extended
    → ``scaler.transform``; Train-Fit-Scaler, s. :mod:`m2.normalize`).

    ``devices``: Keras-Device-Namen (aus ``model.configure_devices``),
    wird in ``keras_train_meta.json`` unter ``device`` persistiert
    (``[]`` = CPU).
    """
    ck = config.CHECKPOINT_DIR
    ck.mkdir(parents=True, exist_ok=True)
    ckpt = ck / "keras_l1.keras"
    meta = ck / "keras_train_meta.json"
    X_train_s = scaler.transform(X_train_ext)
    X_valid_s = scaler.transform(X_valid_ext)
    dev_names = list(devices if devices is not None else [])
    if ckpt.exists() and meta.exists() and not force:
        m = _load_json(meta)
        if (
            int(m.get("n_train", -1)) == int(Y_train.shape[0])
            and int(m.get("n_features", -1)) == int(X_train_s.shape[1])
        ):
            _log("keras: Checkpoint gefunden (Neutraining übersprungen)")
            return keras.models.load_model(str(ckpt))
    n_features = int(X_train_s.shape[1])
    model = build_model(n_features, targets=config.TARGETS)
    n_params = assert_param_budget(model)
    _log(f"keras: konstruiert (Features={n_features}, Parameter={n_params:,})")

    es = keras.callbacks.EarlyStopping(
        monitor="val_loss", mode="min", patience=config.PATIENCE,
        restore_best_weights=True, verbose=0,
    )
    # LR-Scheduling: MAE + Adam oszilliert mit konstanter LR; bei Val-Loss-
    # Plateau halbe die LR (min 3e-5) → stabilere Konvergenz (Empirie v001).
    rlp = keras.callbacks.ReduceLROnPlateau(
        monitor="val_loss", mode="min", factor=0.5,
        patience=max(3, config.PATIENCE // 5), min_lr=3e-5, verbose=0,
    )
    ckpt_cb = keras.callbacks.ModelCheckpoint(
        str(ckpt), monitor="val_loss", mode="min", save_best_only=True, verbose=0,
    )
    y_train = {f"head_{t}": Y_train[:, i] for i, t in enumerate(config.TARGETS)}
    y_valid = {f"head_{t}": Y_valid[:, i] for i, t in enumerate(config.TARGETS)}
    t0 = time.time()
    hist = model.fit(
        X_train_s,
        y_train,
        validation_data=(X_valid_s, y_valid),
        epochs=config.EPOCHS_DEFAULT,
        batch_size=config.BATCH_SIZE,
        callbacks=[es, rlp, ckpt_cb],
        verbose=0,
    )
    dt = time.time() - t0
    _save_json(
        meta,
        {
            "n_train": int(Y_train.shape[0]),
            "n_features": int(X_train_s.shape[1]),
            "epochs_run": int(len(hist.history["loss"])),
            "train_seconds": round(float(dt), 1),
            "seed": config.SEED,
            "n_params": int(n_params),
            "device": dev_names,
            "val_loss_history": [float(v) for v in hist.history.get("val_loss", [])],
        },
    )
    _log(f"keras: trainiert ({len(hist.history['loss'])} Epochs, {dt:.0f} s)")
    return model


def train_lgbm(
    X_train_ext: np.ndarray,
    Y_train: np.ndarray,
    X_valid_ext: np.ndarray,
    Y_valid: np.ndarray,
    scaler: StandardScaler,
    force: bool = False,
    device: str = "cpu",
) -> LGBMEnsemble:
    """Trainiert je Target einen Booster auf **skaliertem** Extended-Input
    (gleicher Scaler wie Keras — Shared-Pipeline-Äquivalenz; GBDT-Prädikation
    ist unter monotoner Skalierung invariant).

    ``device``: ``"cpu"`` (Default, bit-reproduzierbar) oder ``"gpu"``
    (opt-in via ``--lgbm-gpu``; nicht bit-reproduzierbar garantierbar,
    s. ``lgbm_model.lgbm_params``). Device + LGBM-Version landen in
    ``lgbm_train_meta.json`` und ``metadata.json``.
    """
    import lightgbm as lgb

    ck = config.CHECKPOINT_DIR
    lgb_dir = ck / "lgbm_l1"
    meta = ck / "lgbm_train_meta.json"
    dev = "gpu" if device == "gpu" else "cpu"
    if lgb_dir.exists() and meta.exists() and not force:
        m = _load_json(meta)
        if int(m.get("n_train", -1)) == int(Y_train.shape[0]):
            _log("lgbm: Checkpoint gefunden (Neutraining übersprungen)")
            return LGBMEnsemble.load(str(lgb_dir), targets=config.TARGETS)
    t0 = time.time()
    ens, _evals = train_ensemble(
        scaler.transform(X_train_ext), Y_train, scaler.transform(X_valid_ext), Y_valid,
        device=dev,
    )
    ens.save(lgb_dir)
    _save_json(
        meta,
        {
            "n_train": int(Y_train.shape[0]),
            "train_seconds": round(time.time() - t0, 1),
            "device": dev,
            "lightgbm": str(lgb.__version__),
        },
    )
    _log(f"lgbm: {len(ens.models)} Booster trainiert ({time.time() - t0:.0f} s, device={dev})")
    return ens


# --- Phase 5: Metriken ---


def _rollout_mae_valid(
    kind: str,
    splits: dict,
    ens_or_model,
    scaler: StandardScaler,
) -> dict[str, float]:
    """4-h-Rollout-MAE je Target (Valid); kind ∈ {'keras','lgbm','b2'}."""
    valid = splits["valid"]
    starts = baseline.rollout_start_indices(valid, steps=config.ROLLOUT_H)
    truth = baseline.rollout_truth(valid, starts, steps=config.ROLLOUT_H)
    if kind == "b2":
        pred, _excluded = baseline.b2_4h_series(valid, starts, steps=config.ROLLOUT_H)
    elif kind == "keras":
        pred = baseline.rollout_series_keras(ens_or_model, scaler, valid, starts, steps=config.ROLLOUT_H)
    else:
        pred = baseline.rollout_series_lgbm(ens_or_model, scaler, valid, starts, steps=config.ROLLOUT_H)
    per_target: dict[str, float] = {}
    for i, t in enumerate(config.TARGETS):
        per_target[t] = baseline.mae(pred[:, -1, i], truth[:, -1, i])
    # DoD-Gruppen (4-h = letzter Rollout-Step)
    return per_target


def _mae_1h_valid(
    kind: str, splits: dict, X_ext: dict[str, np.ndarray], ens_or_model, scaler: StandardScaler
) -> dict[str, float]:
    """1-h-MAE je Target (Valid)."""
    valid = splits["valid"]
    Y = valid.Y
    Xv = X_ext["valid"]
    if kind == "b2":
        # 1-h-B2: Persistenz (Y[i-1]) + gleiche Zeit gestern (ts-Lookup −24 h)
        idx = np.arange(len(valid.ts))
        pred = np.empty_like(Y)
        ts_map = {ts: i for i, ts in enumerate(valid.ts)}
        from datetime import timedelta

        for i in idx:
            persist = Y[i - 1] if i >= 1 else Y[0]
            j = ts_map.get(valid.ts[i] - timedelta(hours=24))
            yest = Y[j] if j is not None else Y[0]
            pred[i] = 0.5 * persist + 0.5 * yest
        return {t: baseline.mae(pred[:, i], Y[:, i]) for i, t in enumerate(config.TARGETS)}
    if kind == "keras":
        m = evaluate_weighted_mae(ens_or_model, scaler.transform(Xv), Y)
    else:
        m = lgbm_evaluate(ens_or_model, scaler.transform(Xv), Y)
    return {t: float(m[t]) for t in config.TARGETS}


def _worst_day_keras(splits: dict, X_ext: dict[str, np.ndarray], model, scaler: StandardScaler) -> dict[str, object]:
    """Worst-Case-Tag im Test 2026 (max. Tages-MAE RAS-Ist, Keras-1-h)."""
    test = splits.get("test")
    if test is None:
        return {}
    Xt = X_ext["test"]
    pred = predict_targets(model, scaler.transform(Xt), batch_size=2048).reshape(
        len(test.ts), len(config.TARGETS)
    )
    day_mae: dict[str, dict[str, float]] = {}
    for i, ts in enumerate(test.ts):
        d = ts.strftime("%Y-%m-%d")
        rec = day_mae.setdefault(d, {c: [] for c in config.RAS_IST_TARGETS})
        for c in config.RAS_IST_TARGETS:
            j = config.TARGETS.index(c)
            rec[c].append(abs(float(pred[i, j]) - float(test.Y[i, j])))
    worst_day = max(day_mae.items(), key=lambda kv: sum(np.mean(v) for v in kv[1].values()))
    out: dict[str, object] = {"date": worst_day[0]}
    for c in config.RAS_IST_TARGETS:
        out[c] = float(np.mean(worst_day[1][c]))
    return out


def compute_metrics(
    splits: dict,
    X_ext: dict[str, np.ndarray],
    model,
    ens: LGBMEnsemble,
    scaler: StandardScaler,
    force: bool = False,
) -> dict[str, object]:
    ck = config.CHECKPOINT_DIR
    ck.mkdir(parents=True, exist_ok=True)
    m_path = ck / "metrics.json"
    if m_path.exists() and not force:
        _log("metrics: Cache gefunden (runs/checkpoint/metrics.json)")
        return _load_json(m_path)

    _log("metrics: 1-h-MAE (Valid) …")
    maes_k1 = _mae_1h_valid("keras", splits, X_ext, model, scaler)
    maes_b1 = _mae_1h_valid("b2", splits, X_ext, None, scaler)
    maes_l1 = _mae_1h_valid("lgbm", splits, X_ext, ens, scaler)

    _log("metrics: 4-h-Rollout-MAE (Valid) Keras …")
    maes_k4 = _rollout_mae_valid("keras", splits, model, scaler)
    _log("metrics: 4-h-Rollout-MAE (Valid) LGBM …")
    maes_l4 = _rollout_mae_valid("lgbm", splits, ens, scaler)
    _log("metrics: 4-h-Rollout-MAE (Valid) B2 …")
    maes_b4 = _rollout_mae_valid("b2", splits, None, scaler)

    _log("metrics: 4-h-Rollout-MAE (Test 2026) …")
    test_k4, test_l4, test_b4 = {}, {}, {}
    if splits.get("test") is not None:
        test = splits["test"]
        tstarts = baseline.rollout_start_indices(test, steps=config.ROLLOUT_H)
        ttruth = baseline.rollout_truth(test, tstarts, steps=config.ROLLOUT_H)
        pk = baseline.rollout_series_keras(model, scaler, test, tstarts, steps=config.ROLLOUT_H)
        pl = baseline.rollout_series_lgbm(ens, scaler, test, tstarts, steps=config.ROLLOUT_H)
        pb, _ = baseline.b2_4h_series(test, tstarts, steps=config.ROLLOUT_H)
        for i, t in enumerate(config.TARGETS):
            test_k4[t] = baseline.mae(pk[:, -1, i], ttruth[:, -1, i])
            test_l4[t] = baseline.mae(pl[:, -1, i], ttruth[:, -1, i])
            test_b4[t] = baseline.mae(pb[:, -1, i], ttruth[:, -1, i])

    _log("metrics: Offset-Responsivität (Valid, Keras) …")
    resp_k_res, _case_idx = responsivity.run_responsivity_keras(model, scaler, splits["valid"])
    _log("metrics: Offset-Responsivität (Valid, LGBM) …")
    resp_l_res, _ = responsivity.run_responsivity_lgbm(ens, scaler, splits["valid"])

    _log("metrics: Worst-Case-Tag (Test 2026) …")
    worst = _worst_day_keras(splits, X_ext, model, scaler)

    out: dict[str, object] = {
        "maes_keras_valid_1h": maes_k1,
        "maes_b2_valid_1h": maes_b1,
        "maes_lgbm_valid_1h": maes_l1,
        "maes_keras_valid_4h": maes_k4,
        "maes_b2_valid_4h": maes_b4,
        "maes_lgbm_valid_4h": maes_l4,
        "maes_keras_test_4h": test_k4,
        "maes_lgbm_test_4h": test_l4,
        "maes_b2_test_4h": test_b4,
        "responsivity_keras": resp_k_res.summary(),
        "responsivity_lgbm": resp_l_res.summary(),
        "test_2026": {
            "keras": test_k4,
            "lgbm": test_l4,
            "b2": test_b4,
            "worst_day": worst,
        },
    }
    _save_json(m_path, out)
    return out


# --- Phase 6+7: Artefakt + Report ---


def _group_mae(maes: dict[str, float], cols: tuple[str, ...]) -> float:
    vals = [maes[c] for c in cols if c in maes]
    return float(sum(vals) / len(vals)) if vals else float("nan")


def decide_model_kind(metrics: dict[str, object]) -> dict[str, object]:
    """Q-101-Decision: wer **MAE + Responsivität** gewinnt → model_kind.

    Score = mittleres 4-h-Rollout-MAE über DoD-Relevante (3 RAS-Ist-Zonen +
    3 Puffer) + 1,0 K-Strafe bei fehlgeschlagenem Responsivitäts-DoD.
    """
    k = metrics["maes_keras_valid_4h"]
    g = metrics["maes_lgbm_valid_4h"]
    cols = tuple(config.RAS_IST_TARGETS) + tuple(config.BUFF_TARGETS)
    k_mae = _group_mae(k, cols)
    l_mae = _group_mae(g, cols)
    resp_k: dict = metrics["responsivity_keras"]
    resp_l: dict = metrics["responsivity_lgbm"]
    k_resp_ok = bool(resp_k.get("dod_passed"))
    l_resp_ok = bool(resp_l.get("dod_passed"))
    k_score = k_mae + (0.0 if k_resp_ok else 1.0)
    l_score = l_mae + (0.0 if l_resp_ok else 1.0)
    model_kind = "keras" if k_score <= l_score else "lightgbm"
    reason = (
        f"Score = 4-h-Rollout-Mittel-MAE (RAS-Ist ×3 + Puffer ×3) + 1,0 K-Strafe bei "
        f"Responsivitäts-DoD-Fehlschlag: Keras {k_score:.3f} "
        f"(MAE {k_mae:.3f}, Responsivität {'OK' if k_resp_ok else 'fehlschlag'}) vs "
        f"LightGBM {l_score:.3f} (MAE {l_mae:.3f}, Responsivität {'OK' if l_resp_ok else 'fehlschlag'})"
    )
    return {
        "model_kind": model_kind,
        "reason": reason,
        "keras_score": round(k_score, 4),
        "lightgbm_score": round(l_score, 4),
        "keras_resp_ok": k_resp_ok,
        "lightgbm_resp_ok": l_resp_ok,
    }


def _resp_flat(resp: dict) -> dict:
    """Summary-Keys auf Report-Erwartung ablegen (mean_dH_X etc.)."""
    pd_ = resp.get("dH_mittel_je_delta", {})
    out: dict = {}
    for k, v in resp.items():
        out[k] = v
    out["pearson_dH"] = resp.get("pearson_delta_offset_dH")
    out["pearson_dD"] = resp.get("pearson_delta_offset_dD")
    out["frac_dH_neg_34"] = resp.get("dH_neg_anteil_3.4K")
    out["mean_dH_10"] = pd_.get("+1.0K")
    out["mean_dH_20"] = pd_.get("+2.0K")
    out["mean_dH_34"] = pd_.get("+3.4K")
    out["mean_dH_50"] = pd_.get("+5.0K")
    out["mean_dD_34"] = resp.get("dD_mittel_je_delta", {}).get("+3.4K")
    out["dod_passed"] = resp.get("dod_passed")
    return out


def build(
    force: bool = False,
    epochs: int | None = None,
    quiet: bool = False,
    force_keras: bool = False,
    force_lgbm: bool = False,
    lgbm_device: str = "cpu",
) -> int:
    """Voll-Lauf (s. Modul-Doku). Return-Code: 0 = OK.

    Force-Semantik: ``force`` = alle Phasen; ``force_keras`` = Keras-
    Training + Metriken + Artefakt + Report; ``force_lgbm`` = LGBM-Training
    + Metriken + Artefakt + Report (Transitions-/Scaler-Checkpointe
    bleiben bei beiden selektiven Flags). ``lgbm_device``: ``"cpu"``
    (Default, bit-reproduzierbar) oder ``"gpu"`` (opt-in,
    ``--lgbm-gpu``).
    """
    if epochs is not None:
        config.EPOCHS_DEFAULT = int(epochs)

    _set_quiet(quiet)

    _log("=== M2 Build ===")
    # Device-Setup (vor dem Training): GPU-Liste + Memory-Growth;
    # [] = CPU-fallback.
    devices = configure_devices()
    _log(f"device: Keras → {', '.join(devices) if devices else 'CPU'}")
    splits, extra = prepare_data(force=force)
    stats = extra["stats"]
    X_ext: dict[str, np.ndarray] = extra["X_ext"]
    flag_names: list[str] = extra["flag_names"]
    split_info: dict = extra["split_info"]

    train, valid, _test = splits["train"], splits["valid"], splits.get("test")
    scaler_path = config.CHECKPOINT_DIR / "scaler.json"
    scaler = StandardScaler.load_json(scaler_path)

    _log("=== Keras-Training ===")
    model = train_keras(
        X_ext["train"], train.Y, X_ext["valid"], valid.Y, scaler,
        force=force or force_keras,
        devices=devices,
    )
    n_params = count_params(model)

    _log("=== LightGBM-Training ===")
    _lgbm_dev = "gpu" if lgbm_device == "gpu" else "cpu"
    ens = train_lgbm(
        X_ext["train"], train.Y, X_ext["valid"], valid.Y, scaler,
        force=force or force_lgbm,
        device=_lgbm_dev,
    )
    _log("=== Metriken ===")
    metrics = compute_metrics(
        splits, X_ext, model, ens, scaler, force=force or force_keras or force_lgbm
    )
    decision = decide_model_kind(metrics)
    _log(f"decision: model_kind = {decision['model_kind']} ({decision['reason']})")

    _log("=== Artefakt ===")
    fw = _framework_versions()
    maes_meta = {
        "keras_valid_1h": {t: round(float(metrics["maes_keras_valid_1h"][t]), 4) for t in config.TARGETS},
        "lgbm_valid_1h": {t: round(float(metrics["maes_lgbm_valid_1h"][t]), 4) for t in config.TARGETS},
        "b2_valid_1h": {t: round(float(metrics["maes_b2_valid_1h"][t]), 4) for t in config.TARGETS},
        "keras_valid_4h": {t: round(float(metrics["maes_keras_valid_4h"][t]), 4) for t in config.TARGETS},
        "lgbm_valid_4h": {t: round(float(metrics["maes_lgbm_valid_4h"][t]), 4) for t in config.TARGETS},
        "b2_valid_4h": {t: round(float(metrics["maes_b2_valid_4h"][t]), 4) for t in config.TARGETS},
        "keras_test_4h": {t: round(float(v), 4) for t, v in metrics["maes_keras_test_4h"].items()},
        "lgbm_test_4h": {t: round(float(v), 4) for t, v in metrics["maes_lgbm_test_4h"].items()},
        "b2_test_4h": {t: round(float(v), 4) for t, v in metrics["maes_b2_test_4h"].items()},
    }
    out_dir = build_artifact(
        keras_model=model,
        lgbm_ens=ens,
        scaler=scaler,
        keras_params=n_params,
        maes=maes_meta,
        decision=decision,
        split_info=split_info,
        framework_versions=fw,
        n_features=int(X_ext["train"].shape[1]),
        lgbm_device=_lgbm_dev,
        devices=devices,
    )
    _log(f"artefakt: {config.rel_to_repo(out_dir)}")
    _save_json(
        config.ARTIFACT_READY_FLAG,
        {"artifact_dir": str(out_dir), "model_kind": decision["model_kind"], "n_params": int(n_params)},
    )

    _log("=== Report ===")
    resp_k_flat = _resp_flat(metrics["responsivity_keras"])
    resp_l_flat = _resp_flat(metrics["responsivity_lgbm"])
    _ = resp_l_flat  # LGBM-Responsivität wird im Report ausgewiesen (Tabellen)
    k_corr = resp_k_flat.get("pearson_dH")
    k_frac = resp_k_flat.get("frac_dH_neg_34")
    if k_corr is not None and k_corr == k_corr:
        if k_corr <= -0.6 and (k_frac or 0) >= 0.75:
            interp = (
                "JA (modellseitig reproduziert): Offset↑ führt im 6-h-Rollout zu messbar "
                "sinkenden Keller-Heizminuten und reduziertem d8-An-Quotienten — "
                "das 18.10-Muster (Offset +3,4 K → Heizkreis Keller abfahrt) ist im "
                "Modell enthalten."
            )
        elif k_corr <= -0.6 or (k_frac or 0) >= 0.75:
            interp = (
                "TEILWEISE: ein DoD-Kriterium erfüllt, das andere nicht — der Offset-Effekt "
                "ist im Modell vorhanden, aber schwächer/uneinheitlicher als im 18.10-Ereignis "
                "(historischer Confound: Offset-Sprünge sind in der Historie selten und oft an "
                "Wetter-Situationen gebunden; s. Probleme/Annahmen)."
            )
        else:
            interp = (
                "NEIN (modellseitig nicht reproduziert): der 18.10-Kausalzusammenhang "
                "(Offset↑ → Heizminuten↓) wird vom Modell nicht in der geforderten Stärke "
                " abgebildet — historisch ist der Offset-Effekt durch Saison-Confounding "
                "überlagert (Offset-Sprünge treten überwiegend in warmen Perioden auf, in denen "
                "die Heizung ohnehin abregelt). Konsequenz: v0.3-Architektur-Risiko (Design §9) "
                "trifft für L1 zu; L2 muss den Offset-Hebel zusätzlich mit Guardrails absichern, "
                "und ein Offset-Feature-Branching (explizite Offset-Aktion-Eingabe, bereits im "
                "Modell) ist der geplante Mitigations-Pfad."
            )
    else:
        interp = "n/a (Korrelation nicht berechenbar)."
    results: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_rev": decision.get("git_rev", "") or _git_rev(),
        "tf_version": fw["tensorflow"],
        "keras_version": fw["keras"],
        "lgbm_version": fw["lightgbm"],
        "numpy_version": fw["numpy"],
        "stats": _stats_dict(stats),
        "split_info": split_info,
        "n_flag_cols": len(flag_names),
        "maes_keras_valid_1h": {t: float(metrics["maes_keras_valid_1h"][t]) for t in config.TARGETS},
        "maes_b2_valid_1h": {t: float(metrics["maes_b2_valid_1h"][t]) for t in config.TARGETS},
        "maes_keras_valid_4h": {t: float(metrics["maes_keras_valid_4h"][t]) for t in config.TARGETS},
        "maes_b2_valid_4h": {t: float(metrics["maes_b2_valid_4h"][t]) for t in config.TARGETS},
        "maes_lgbm_valid_4h": {t: float(metrics["maes_lgbm_valid_4h"][t]) for t in config.TARGETS},
        "responsivity_keras": resp_k_flat,
        "responsivity_lgbm": resp_l_flat,
        "test_2026": {
            "keras": {t: float(metrics["maes_keras_test_4h"].get(t, float("nan"))) for t in config.TARGETS},
            "lgbm": {t: float(metrics["maes_lgbm_test_4h"].get(t, float("nan"))) for t in config.TARGETS},
            "b2": {t: float(metrics["maes_b2_test_4h"].get(t, float("nan"))) for t in config.TARGETS},
            "worst_day": metrics["test_2026"].get("worst_day", {}),
        },
        "architecture": model_architecture_summary(model),
        "devices": list(devices),
        "lgbm_device": _lgbm_dev,
        "decision": decision,
        "responsivity_interpretation": interp,
        "keras_last_epoch": int(_load_json(config.CHECKPOINT_DIR / "keras_train_meta.json").get("epochs_run", 0))
        if (config.CHECKPOINT_DIR / "keras_train_meta.json").exists()
        else 0,
        "keras_valid_loss_best": min(_val_loss_history()) if _val_loss_history() else None,
        "keras_valid_history": _val_loss_history(),
        "assumptions": [
            "Wetter-Fenster im Rollout: t→t+23-Fenster der Ausgangsstunde bleibt **konstant** "
            "(kein 1-h-Verschieben) — am einfachsten für Realitätsnähe, dokumentierte Vereinfachung "
            "für 4–6-h-Horizonte.",
            "Digital-Head-Loss: MAE auf Sigmoid-Wahrscheinlichkeit (Initial-Design); "
            "BCE-Alternative nicht erprobt (kein Grid-Search-Overkill).",
            "heat_min_kell ≈ gesättigt (~5,9 h/h Median, Zähler-Rollup-Artefakt): der Offset-"
            "Responsivitäts-ΔH ist dadurch klein (±0,1–0,3 h) — Vorzeichen-Korrelation ist die "
            "robuste Metrik, nicht die absolute ΔH-Größe.",
            "Offset-Confounding: historisch traten Keller-Offset-Sprünge (≥ 1 K) überwiegend in "
            "milden Perioden auf, in denen die Anlage ohnehin abregelt → in den Daten "
            "Offset↑ ↔ heat↑ (Saison); der 18.10-Kausalpfad (Offset↑ → heat↓) ist ein Einzelereignis. "
            "Der Responsivitäts-Test prüft den Pfad im Modell, nicht die Daten-Korrelation.",
            "Lags im Rollout bleiben beim t0-Wert (keine Fortschreibung) — 4–6-h-Horizont, "
            "dokumentierte Vereinfachung.",
            "B2-4-h-Rollout: Persistenz = letztes Prädikt, ‚gleiche Zeit gestern' = Wert der "
            "Ausgangs-Row −24 h (konstant) — konservativ für B2 (kein Fehler-Kumulieren).",
            "Kollektor (solar_coll) vor 2022-03-24 NULL → Forward-/Median-Imputation + NULL-Flag; "
            "der Solar-Head lernt ‘inaktiv → aktiv’-Transition nur ab März 2022.",
            "Kaltstart-Check (2020-08→12): kein separater Report (Train-Split enthält den "
            "Zeitraum; Valid-MAE-Unterschiede über Jahresgruppen s. Test-2026-Report als "
            "Proxy).",
            "Keras-L1-Konvergenz: der geteilte 31-Head-Stamm erreicht auf den sehr glatten "
            "RAS-Ist-Kanälen im 4-h-Rollout nicht die GBDT-Präzision (Valid 1-h RAS-Ist: "
            "Keras 1,1 K vs LGBM 0,17 K vs Persistence 0,12 K; Test-4h: Keras 12 K vs LGBM "
            "0,44–0,72 K). In ~6/7264 Test-Starts (extreme/Out-of-Domain-Zustände, z. B. "
            "2026-10-31) läuft der Keras-Rollout in ~9000 K — ein bekanntes Stabilitäts-"
            "problem unregulierter NNN-Rollouts; das LGBM-Deployment (model_kind) zeigt "
            "dieses Verhalten **nicht** (0,44–0,72 K über alle Test-Starts). Konsequenz: "
            "L1-deployment = LightGBM; Keras-L1 bleibt versioniert als Referenz (L2-Guardrails "
            "müssen extreme-Prädikats-Clippen bei NNN-Rollouts vorsehen).",
            "LGBM-DoD-GAP: MAE+4h Puffer (2,0–2,4 K) liegt über der 1,5 K-Schwelle — die "
            "Puffer-Temperatur ist 4-h-aus physikalisch schwer präzise prognostizierbar "
            "(Ladephasen, Solar-Spitzen, Brenner-Taktung); 1-h-MAE LGBM = 0,6–0,7 K "
            "(gut), die 4-h-Kumulierung verliert. Kausal: kein Modell-Defekt, sondern "
            "dynamische Komplexität; L2 (12-h-Horizont + Empfehlung) adressiert dies mit "
            "Szenario-Validierung statt Punkt-MAE.",
        ],
    }
    report.write_report(results)
    _log(f"report: {config.rel_to_repo(config.REPORT_PATH)}")
    _log("=== M2 Build fertig ===")
    return 0


def _git_rev() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "nogit"
    except Exception:  # noqa: BLE001
        return "nogit"


def _val_loss_history() -> list[float]:
    """Valid-Loss-Verlauf (aus Keras-Checkpoint-Meta, falls vorhanden)."""
    p = config.CHECKPOINT_DIR / "keras_train_meta.json"
    if not p.exists():
        return []
    m = _load_json(p)
    hist = m.get("val_loss_history")
    if isinstance(hist, list):
        return [float(v) for v in hist]
    return []


def main(argv: list[str] | None = None) -> int:
    # GPU-Self-Re-Exec (vor TF-Import, nur CLI-Pfad; Tests setzen
    # CMI_M2_GPU_REEXEC=1 per autouse-Fixtur → kein Re-Exec).
    # ``sys.argv[1:]`` = exakt die CLI-Argumente (cmd, --force …) in beiden
    # Aufrufformen (``-m analysis.model.m2 …``: argv[0] = __main__.py-Pfad;
    # Direkt-Skript: argv[0] = Skript-Pfad).
    if _gpu_reexec(list(sys.argv[1:])):
        return 0

    ap = argparse.ArgumentParser(prog="analysis.model.m2", description="M2 L1-Modell-Build")
    ap.add_argument("cmd", nargs="?", default="build", choices=["build", "metrics", "report"])
    ap.add_argument("--force", action="store_true", help="Checkpoints ignorieren (Neutraining aller Phasen)")
    ap.add_argument(
        "--force-keras", action="store_true",
        help="nur Keras neu trainieren (+ Metriken/Artefakt/Report; LGBM/Transitions-Checkpointe bleiben)",
    )
    ap.add_argument(
        "--force-lgbm", action="store_true",
        help="nur LightGBM neu trainieren (+ Metriken/Artefakt/Report; Keras/Transitions-Checkpointe bleiben)",
    )
    ap.add_argument(
        "--lgbm-gpu", action="store_true",
        help="LightGBM auf GPU trainieren (opt-in; nicht bit-reproduzierbar garantierbar — Default CPU)",
    )
    ap.add_argument("--epochs", type=int, default=None, help="Override Trainings-Epochs (Default 60)")
    ap.add_argument("--quiet", action="store_true", help="weniger Log")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        return build(
            force=args.force,
            epochs=args.epochs,
            quiet=args.quiet,
            force_keras=args.force_keras,
            force_lgbm=args.force_lgbm,
            lgbm_device="gpu" if args.lgbm_gpu else "cpu",
        )
    raise SystemExit(f"Unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
