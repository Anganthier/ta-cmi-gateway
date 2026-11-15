"""M2b — Trainings-Pipeline v003 (Daten → LightGBM → Keras v2 → Bewertung → Artefakt).

Ablauf (``python -m analysis.model.m2b train``):

1. Feature-Store m1.3 laden, Features ableiten (:mod:`m2b.data`, :mod:`m2b.features`).
2. LightGBM je (Target, h): Train + Early-Stopping auf Valid 2025. Für
   ``MONOTONE_TARGETS`` zusätzlich Variante mit Monotonie-Vorgaben; gewählt
   wird die monotone Variante, wenn ihr Valid-Fehler ≤ ``MONO_TOLERANCE`` ×
   Fehler der freien Variante ist (Kausalität kostet höchstens 2 %). Fehler =
   MAE (analog) bzw. RMSE (digital, s. ``evaluate``).
3. Keras v2 (ein Netz für alle (Target, h)-Ausgaben).
4. Bewertung je Split (MAE vs. B0/B0-last/B2), DoD, Responsivität v2.
5. Gewinner nach Q-101 (Responsivität besteht → Vorrang; sonst mittlerer
   Skill vs. B0 auf Valid).
6. LightGBM-Refit auf Train+Valid (Deploy-Modelle, Test 2026 separat bewertet).
7. Artefakt ``models/cmi-brain/v003/`` + ``REPORT.md`` (inkl. Vergleich mit v002 aus
   ``models/cmi-brain/v002/summary.json``, flue ×10, s. :func:`m2b.report.load_reference`).
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config, evaluate, responsivity
from .data import SPLITS, Dataset, build_dataset
from .features import DERIVE_VERSION, DERIVED_F3
from .lgbm import LgbmL1, feature_importance, fit_one, importance_share, refit_full

MONO_TOLERANCE = 1.02


def select_metric(target: str) -> str:
    """Maßgebliche Fehlermetrik für Varianten-Wahl/Report (analog MAE, digital RMSE)."""
    return "rmse" if config.TARGET_KIND[target] == "digital" else "mae"


def _git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=str(config._REPO_ROOT), timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:  # pragma: no cover
        return "unknown"


def _score_all(ds: Dataset, keys, pred_out: Callable[[str, int], np.ndarray], splits=SPLITS) -> dict:
    """``pred_out(t, h)`` = Modell-Ausgabe für alle Rasterzeilen → Metriken je Split."""
    res: dict = {}
    for t, h in keys:
        spec = ds.target(t, h)
        pa = spec.to_abs(pred_out(t, h))
        res[f"{t}@{h}"] = {s: evaluate.score(ds, spec, pa, s) for s in splits if ds.rows(s).any()}
    return res


def run_train(
    features_dir: Path | None = None,
    out_dir: Path | None = None,
    targets: tuple[str, ...] = config.TARGETS,
    horizons: tuple[int, ...] = config.HORIZONS,
    with_keras: bool = True,
    with_refit: bool = True,
    with_monotone: bool = True,
    threads: int = config.LGBM_THREADS_DEFAULT,
    num_boost: int = config.LGBM_NUM_BOOST,
    keras_epochs: int = config.KERAS_EPOCHS,
    check_version: bool = True,
    resp_cases: int = config.RESP_N_CASES,
    write_report: bool = True,
    report_path: Path | None = None,
    train_start: str | None = config.TRAIN_START,
    reference_dir: Path | None = config.ARTIFACT_V002_DIR,
    log: Callable[[str], None] = print,
) -> dict:
    """Komplett-Lauf; liefert die Zusammenfassung (auch als ``summary.json``)."""
    t_start = time.time()
    out = Path(out_dir) if out_dir is not None else config.ARTIFACT_DIR
    ds = build_dataset(features_dir, check_version=check_version, train_start=train_start)
    log(f"[m2b] Daten: {ds.n} Stunden, {ds.X.shape[1]} Features, data_version={ds.data_version}, "
        f"Splits " + ", ".join(f"{s}={int(ds.rows(s).sum())}" for s in SPLITS))
    keys = [(t, h) for t in targets for h in horizons]

    # --- LightGBM ---
    eval_lgbm = LgbmL1()
    plain_lgbm = LgbmL1()
    variant: dict[str, dict] = {}
    timings: dict[str, float] = {}
    t0 = time.time()
    for i, (t, h) in enumerate(keys):
        tk = time.time()
        b, info = fit_one(ds, t, h, monotone=False, num_boost=num_boost, threads=threads)
        plain_lgbm.boosters[(t, h)] = b
        plain_lgbm.infos[(t, h)] = info
        spec = ds.target(t, h)
        key = select_metric(t)
        mae_plain = evaluate.score(ds, spec, spec.to_abs(plain_lgbm.predict(t, h, ds.X)), "valid")[key]
        chosen, cinfo, mono_mae = b, info, None
        if with_monotone and t in config.MONOTONE_TARGETS:
            bm, im = fit_one(ds, t, h, monotone=True, num_boost=num_boost, threads=threads)
            tmp = LgbmL1(boosters={(t, h): bm})
            mono_mae = evaluate.score(ds, spec, spec.to_abs(tmp.predict(t, h, ds.X)), "valid")[key]
            forced = config.CONTROLLER_CAUSAL and t in config.CONTROLLER_TARGETS
            if forced or mono_mae <= MONO_TOLERANCE * mae_plain:
                chosen, cinfo = bm, im
        eval_lgbm.boosters[(t, h)] = chosen
        eval_lgbm.infos[(t, h)] = cinfo
        variant[f"{t}@{h}"] = {
            "monotone": cinfo.monotone, "best_iter": cinfo.best_iter,
            "metric": key, "valid_mae_plain": mae_plain, "valid_mae_mono": mono_mae,
            "n_train": cinfo.n_train, "n_valid": cinfo.n_valid,
        }
        log(f"[m2b] LGBM {i + 1}/{len(keys)} {t}@{h}h it={cinfo.best_iter} mono={cinfo.monotone} "
            f"valid_{key}={mae_plain if not cinfo.monotone else mono_mae:.4f} ({time.time() - tk:.0f}s)")
    timings["lgbm_s"] = round(time.time() - t0, 1)

    metrics: dict[str, dict] = {}
    metrics["lgbm"] = _score_all(ds, keys, lambda t, h: eval_lgbm.predict(t, h, ds.X))
    if with_monotone:
        metrics["lgbm_plain"] = _score_all(ds, keys, lambda t, h: plain_lgbm.predict(t, h, ds.X))

    resp: dict[str, dict] = {}
    available = set(targets)
    resp_zones = tuple(z for z in config.RESP_ZONES
                       if any(e in available for e in config.resp_effects(z)))
    can_resp = bool(resp_zones) and all(h in horizons for h in config.RESP_HORIZONS)

    def _resp(pred, split="valid"):
        return responsivity.run_all_zones(ds, pred, available, split=split, n_cases=resp_cases,
                                          zones=resp_zones)

    if can_resp:
        resp["lgbm"] = _resp(eval_lgbm.predict)
        if with_monotone:
            resp["lgbm_plain"] = _resp(plain_lgbm.predict)
        log("[m2b] Responsivität LGBM: " + "; ".join(
            f"{z}: {resp['lgbm'][z]['primary']} r={resp['lgbm'][z]['pearson_primary']}" for z in resp_zones))

    # --- Keras v2 ---
    keras_l1 = None
    if with_keras:
        from .keras_model import train as keras_train

        t0 = time.time()
        keras_l1 = keras_train(ds, keys, epochs=keras_epochs)
        timings["keras_s"] = round(time.time() - t0, 1)
        P = keras_l1.predict_all(ds.X)
        kidx = {k: j for j, k in enumerate(keras_l1.prep.out_keys)}
        metrics["keras"] = _score_all(ds, keys, lambda t, h: P[:, kidx[(t, h)]])
        if can_resp:
            resp["keras"] = _resp(keras_l1.predict)
        log(f"[m2b] Keras v2: {len(keras_l1.history.get('loss', []))} Epochen, {timings['keras_s']}s")

    # --- DoD + Gewinner ---
    dod = {m: evaluate.dod_check(metrics, m) for m in metrics}
    skill = {m: evaluate.mean_skill(metrics[m]) for m in metrics}
    cands = [m for m in ("lgbm", "keras") if m in metrics]
    passing = [m for m in cands if resp.get(m, {}).get("dod_passed")]
    pool = passing if passing else cands
    winner = max(pool, key=lambda m: (skill[m] if np.isfinite(skill[m]) else -1e9))
    log(f"[m2b] Skill vs B0 (valid): {skill} → Gewinner: {winner}")

    # --- Refit (Deploy) ---
    deploy = eval_lgbm
    if with_refit:
        t0 = time.time()
        deploy = LgbmL1()
        for t, h in keys:
            info = eval_lgbm.infos[(t, h)]
            b, ri = refit_full(ds, t, h, info.best_iter, monotone=info.monotone, threads=threads)
            deploy.boosters[(t, h)] = b
            deploy.infos[(t, h)] = ri
        timings["refit_s"] = round(time.time() - t0, 1)
        metrics["lgbm_refit"] = _score_all(ds, keys, lambda t, h: deploy.predict(t, h, ds.X), splits=("test",))
        if can_resp:
            # Refit kennt Valid 2025 (in-sample) → Responsivität out-of-sample auf Test 2026;
            # Vergleichswert: Bewertungsmodell auf denselben Test-Fällen.
            resp["lgbm_refit@test"] = _resp(deploy.predict, split="test")
            resp["lgbm@test"] = _resp(eval_lgbm.predict, split="test")

    imp_keys = [(t, h) for t, h in keys if h == config.DOD_HORIZON or len(horizons) == 1]
    importance = {
        f"{t}@{h}": feature_importance(eval_lgbm.boosters[(t, h)], ds.feature_names, top=8)
        for t, h in imp_keys
    }
    # Neue v003-Merkmale (Rohspalten m1.3 + Ableitungen m2b-f3): Gain-Anteil gesamt + Top 10.
    new_feats = [n for n in ds.feature_names if n in set(config.V003_NEW_RAW_INPUTS) | set(DERIVED_F3)]
    importance_new = {}
    for t, h in imp_keys:
        tot, top = importance_share(eval_lgbm.boosters[(t, h)], ds.feature_names, new_feats, top=10)
        importance_new[f"{t}@{h}"] = {"share_total": tot, "top": top}
    from .report import load_reference

    reference = load_reference(reference_dir) if reference_dir is not None else None
    timings["total_s"] = round(time.time() - t_start, 1)
    summary = {
        "artifact": config.ARTIFACT_NAME,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "git_rev": _git_rev(),
        "data_version": ds.data_version,
        "derive_version": DERIVE_VERSION,
        "seed": config.SEED,
        "n_hours": ds.n,
        "n_raw_inputs": len(config.RAW_INPUTS),
        "n_features": int(ds.X.shape[1]),
        "n_new_features": len(new_feats),
        "n_nonhourly_steps": ds.n_nonhourly_steps,
        "split_rows": {s: int(ds.rows(s).sum()) for s in ("pre",) + SPLITS},
        "train_start": train_start,
        "targets": list(targets),
        "horizons": list(horizons),
        "target_kind": {t: config.TARGET_KIND[t] for t in targets},
        "winner": winner,
        "deploy_lgbm": "refit_train_valid" if with_refit else "train_only",
        "mean_skill_valid": skill,
        "dod": dod,
        "responsivity": resp,
        "lgbm_variants": variant,
        "importance": importance,
        "importance_new": importance_new,
        "reference_v002": reference,
        "metrics": metrics,
        "timings": timings,
        "config": {
            "ras_stale_age": config.RAS_STALE_AGE,
            "controller_targets": list(config.CONTROLLER_TARGETS),
            "controller_causal": config.CONTROLLER_CAUSAL,
            "excluded_features": {t: config.excluded_features(t, ds.feature_names) for t in targets
                                  if config.excluded_features(t, ds.feature_names)},
            "mono_tolerance": MONO_TOLERANCE,
            "lgbm_params_analog": config.lgbm_params("analog", threads=threads),
            "lgbm_num_boost": num_boost,
            "keras_hidden": list(config.KERAS_HIDDEN),
            "keras_epochs_max": keras_epochs,
        },
    }
    _write_artifact(out, ds, deploy, keras_l1, summary)
    if write_report:
        from .report import write_report as _wr

        _wr(summary, report_path or config.REPORT_PATH)
    log(f"[m2b] fertig in {timings['total_s']}s → {out}")
    return summary


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_jsonable(v) for v in o]
    if isinstance(o, float | np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _write_artifact(out: Path, ds: Dataset, deploy: LgbmL1, keras_l1, summary: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    deploy.save(out / "lgbm")
    if keras_l1 is not None:
        keras_l1.save(out / "keras")
    (out / "features.json").write_text(json.dumps({
        "derive_version": DERIVE_VERSION,
        "raw_inputs": list(config.RAW_INPUTS),
        "feature_names": ds.feature_names,
    }, indent=1))
    meta = {k: summary[k] for k in (
        "artifact", "created_at", "git_rev", "data_version", "derive_version", "seed",
        "train_start", "split_rows",
        "targets", "horizons", "target_kind", "winner", "deploy_lgbm", "mean_skill_valid",
        "dod", "responsivity", "timings", "config",
    )}
    meta["lgbm_monotone"] = {k: v["monotone"] for k, v in summary["lgbm_variants"].items()}
    meta["lgbm_files"] = sorted(p.name for p in (out / "lgbm").glob("*.txt"))
    (out / "metadata.json").write_text(json.dumps(_jsonable(meta), indent=1))
    (out / "summary.json").write_text(json.dumps(_jsonable(summary), indent=1))
