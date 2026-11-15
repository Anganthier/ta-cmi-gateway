"""M2 — Artefakt-Bau ``models/cmi-brain/v001/``.

Layout:
- ``keras_l1.keras``      Keras-3-Modell (TF-Backend, best-epoch-Weights).
- ``lgbm_l1/{target}.txt`` LightGBM-Booster je Target (31 Dateien).
- ``scaler.json``         StandardScaler (Train-Fit; Keras **und** LGBM).
- ``metadata.json``       data_version, git-Rev, seed, Framework-Versionen,
                          Split-Perioden, Parameter-Count, MAEs, model_kind.
- ``README.md``           1 Absatz: was ist was, wie inferieren.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config
from .normalize import StandardScaler


def _git_tag() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=str(config._REPO_ROOT),
        )
        tag = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10,
            cwd=str(config._REPO_ROOT),
        ).stdout.strip()
        return f"{tag}{'-dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001 - kein git → Fallback
        return "nogit"


def write_keras(model, path: Path) -> None:
    model.save(str(path))


def write_lgbm(ens, out_dir: Path) -> dict[str, Path]:
    return ens.save(out_dir)


def write_scaler(scaler: StandardScaler, path: Path) -> None:
    scaler.save_json(path)


def build_metadata(
    keras_params: int,
    maes: dict[str, dict[str, object]],
    decision: dict[str, object],
    scaler: StandardScaler,
    split_info: dict[str, object],
    framework_versions: dict[str, str],
    n_features: int,
    lgbm_device: str = "cpu",
    devices: list[str] | None = None,
) -> dict[str, object]:
    return {
        "artifact": config.ARTIFACT_NAME,
        "data_version": config.DATA_VERSION,
        "git_rev": _git_tag(),
        "seed": config.SEED,
        "frameworks": framework_versions,
        "device": list(devices) if devices is not None else [],
        "lgbm_device": "gpu" if lgbm_device == "gpu" else "cpu",
        "train_end": str(split_info.get("train_end", config.TRAIN_END))[:10],
        "valid_period": [str(split_info.get("valid_start", config.VALID_START))[:10],
                         str(split_info.get("valid_end", config.VALID_END))[:10]],
        "test_period": [str(split_info.get("test_start", config.TEST_START))[:10],
                        str(split_info.get("test_end", "2099-01-01"))[:10]],
        "param_count": int(keras_params),
        "n_features": int(n_features),
        "n_targets": len(config.TARGETS),
        "scaler": {
            "kind": "standard_scaler",
            "n_features": int(scaler.n_features),
            "n_samples_seen": int(scaler.n_samples_seen),
        },
        "maes": maes,
        "decision": decision,
    }


def write_metadata(meta: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


README_TEMPLATE = """# cmi-brain v001 — L1-Anlagen-Modell (1-h-Step, action-conditional)

**Was ist was:** ``keras_l1.keras`` = Keras-3-Modell (geteilter Stamm 256/128/64 +
einer Head pro Target, gewichtetes MAE, Early-Stopping auf Valid), trainiert
auf dem M1-Feature-Store (``data_version=m1.0``), deterministischer Seed
{seed}. ``lgbm_l1/`` = LightGBM-Pflicht-Vergleich (Q-101), ein Booster je
Target, deterministisch. ``scaler.json`` = StandardScaler (Train-Fit) — bei
**beiden** Modellfamilien vor der Inferenz anwenden (Keras: ``scaler.transform``;
LightGBM: optional, GBDT ist invariant unter monotoner Skalierung, wird aber
für Shared-Pipeline-Äquivalenz beibehalten). ``metadata.json`` =
Versions-Meta (git-Rev, Framework-Versionen, Splits, MAEs, ``model_kind``
= gewinnendes Modell nach DoD). **Wie inferieren:** Feature-Vektor nach
``analysis/model/m2/config.py`` (``FEATURE_INPUTS``-Reihenfolge: State +
Lags 1/2/6/12/24 + Digital-Actions + RAS-Offsets + 24-h-Wetter-Fenster
(bias-korrigierte T) + Kalender + Flags + NULL-Flags), skalieren mit
``scaler.json``, 1-h-Prädikat = Modell-Output (31 Targets,
``config.TARGETS``-Reihenfolge); Multi-h-Horizonte per sequenziellen
Rollout (s. ``analysis/model/m2/rollout.py``). Deployment-Modell =
``model_kind`` aus ``metadata.json`` (Keras oder LightGBM).
"""


def write_readme(path: Path) -> None:
    path.write_text(README_TEMPLATE.format(seed=config.SEED))


def build_artifact(
    keras_model,
    lgbm_ens,
    scaler: StandardScaler,
    keras_params: int,
    maes: dict[str, dict[str, object]],
    decision: dict[str, object],
    split_info: dict[str, object],
    framework_versions: dict[str, str],
    n_features: int,
    out_dir: Path | None = None,
    lgbm_device: str = "cpu",
    devices: list[str] | None = None,
) -> Path:
    """Baut das komplette Artefakt unter ``out_dir`` (Default v001).

    ``lgbm_device``/``devices`` landen in ``metadata.json``
    (``lgbm_device`` = cpu|gpu; ``device`` = Keras-Device-Namen,
    ``[]`` = CPU).
    """
    out = Path(out_dir) if out_dir is not None else config.ARTIFACT_DIR
    out.mkdir(parents=True, exist_ok=True)
    lgbm_dir = out / "lgbm_l1"
    write_keras(keras_model, out / "keras_l1.keras")
    write_lgbm(lgbm_ens, lgbm_dir)
    write_scaler(scaler, out / "scaler.json")
    meta = build_metadata(
        keras_params=keras_params,
        maes=maes,
        decision=decision,
        scaler=scaler,
        split_info=split_info,
        framework_versions=framework_versions,
        n_features=n_features,
        lgbm_device=lgbm_device,
        devices=devices,
    )
    write_metadata(meta, out / "metadata.json")
    write_readme(out / "README.md")
    return out
