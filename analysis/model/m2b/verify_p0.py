"""M2b-P0-Nachweis: v001-Keras mit altem vs. repariertem Scaler (``m2.normalize``).

Lauf: ``python -m analysis.model.m2b.verify_p0 [--features-dir data/features_m1.0]``

v001 wurde auf dem m1.0-Store trainiert → Default-Store = Backup ``data/features_m1.0``.
Gibt je Split max. |Vorhersage| und 1-h-MAE ausgewählter Targets für
(a) alten Transform (std = 1e-4 für konstante Spalten, keine Kappung) und
(b) reparierten Transform (konstante Spalten std = 1, Kappung ±10) aus und
schreibt ``analysis/model/m2b/runs/verify_p0.json``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np

from . import config

TGTS = ("ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "buff_top", "buff_mid", "hz_vl")


def run(features_dir: Path, art_dir: Path) -> dict:
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import keras

    from analysis.model.m2 import config as m2c
    from analysis.model.m2.normalize import StandardScaler
    from analysis.model.m2.transitions import build_transitions, with_null_flags

    splits, _ = build_transitions(features_dir=features_dir)
    sc = StandardScaler.load_json(art_dir / "scaler.json")
    model = keras.models.load_model(str(art_dir / "keras_l1.keras"), compile=False)
    out: dict = {"features_dir": str(features_dir), "artifact": str(art_dir), "splits": {}}
    for name in ("valid", "test"):
        X, _ = with_null_flags(splits[name])
        Y = splits[name].Y
        z_old = ((np.asarray(X, dtype=np.float64) - sc.mean_) / sc.std_).astype(np.float32)
        z_new = sc.transform(X)
        res = {"n": int(len(X)), "max_abs_z_old": float(np.abs(z_old).max()), "max_abs_z_new": float(np.abs(z_new).max())}
        for tag, Z in (("old", z_old), ("new", z_new)):
            P = model.predict(Z, batch_size=4096, verbose=0)
            P = np.concatenate([np.asarray(p).reshape(len(X), -1) for p in P], axis=1) if isinstance(P, list) else np.asarray(P)
            res[f"max_abs_pred_{tag}"] = float(np.abs(P).max())
            res[f"n_pred_gt_{int(config.PHYS_ABS_MAX)}_{tag}"] = int((np.abs(P) > config.PHYS_ABS_MAX).any(axis=1).sum())
            for t in TGTS:
                j = m2c.TARGETS.index(t)
                ok = np.isfinite(Y[:, j])
                res[f"mae1h_{t}_{tag}"] = float(np.mean(np.abs(P[ok, j] - Y[ok, j])))
        out["splits"][name] = res
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="m2b.verify_p0")
    ap.add_argument("--features-dir", type=Path, default=config._REPO_ROOT / "data" / "features_m1.0")
    ap.add_argument("--model", type=Path, default=config.ARTIFACT_V001_DIR)
    ap.add_argument("--out", type=Path, default=config.RUNS_DIR / "verify_p0.json")
    a = ap.parse_args(argv)
    res = run(a.features_dir, a.model)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
