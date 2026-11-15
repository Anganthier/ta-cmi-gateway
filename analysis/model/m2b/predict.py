"""M2b — Vorhersage mit einem gespeicherten L1-Artefakt (v003 Default, v002 weiter nutzbar).

Kurz (Details + Beispiele: ``docs/l1-model-v003.md`` §7)::

    from analysis.model.m2b.predict import L1Predictor
    p = L1Predictor.load()                      # models/cmi-brain/v003, Gewinner-Backend
    res = p.predict_at("2026-10-30 18:00")      # {h: {target: Wert}}
    res = p.predict_at("2026-10-30 18:00", offset_delta={"eg": 2.0})  # Szenario

    p2 = L1Predictor.load("models/cmi-brain/v002")   # Rollback
    p2.predict_at("2025-10-30 18:00", features_dir="data/features_m1.2")

CLI::

    python -m analysis.model.m2b predict --ts "2026-10-30 18:00" [--offset-eg 2.0]
    python -m analysis.model.m2b predict --ts "..." --model models/cmi-brain/v002 \\
        --features-dir data/features_m1.2

Rohspalten und Feature-Namen kommen **aus dem Artefakt** (``features.json``):
:func:`m2b.features.derive` mit ``names=…`` erzeugt genau dessen Spalten, aus dem
Store wird nur gelesen, was dafür nötig ist. Die ``data_version`` des Stores muss
zur ``data_version`` des Artefakts passen (sonst ``ValueError`` mit Hinweis auf den
passenden Store; z. B. v002 → ``--features-dir data/features_m1.2``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config
from .data import load_store
from .features import apply_offset_step, derive, required_raw


def artifact_data_version(meta: dict, summary: dict | None = None) -> str:
    """``data_version`` eines Artefakts (metadata → summary → Default ``m1.2``)."""
    for src in (meta, summary or {}):
        v = src.get("data_version")
        if v:
            return str(v)
    return config.ARTIFACT_DATA_VERSION_DEFAULT


def check_data_version(store_dv: str, art_dv: str, artifact: str = "") -> None:
    """Versionssperre: Store und Artefakt müssen dieselbe ``data_version`` haben."""
    if store_dv == art_dv:
        return
    hint = config.FEATURES_DIR_HINT.get(art_dv)
    tip = f" → `--features-dir {hint}`" if hint else ""
    raise ValueError(
        f"Feature-Store data_version={store_dv!r} passt nicht zum Artefakt {artifact or '?'} "
        f"(trainiert auf {art_dv!r}){tip}. Beispiel Rollback: v002 → "
        f"`--model models/cmi-brain/v002 --features-dir {config.FEATURES_DIR_HINT['m1.2']}`."
    )


@dataclass
class L1Predictor:
    art_dir: Path
    backend: str  # 'lgbm' | 'keras'
    meta: dict
    feature_names: list[str]
    raw_inputs: list[str] = field(default_factory=list)
    data_version: str = config.ARTIFACT_DATA_VERSION_DEFAULT
    lgbm: object | None = None
    keras: object | None = None

    @classmethod
    def load(cls, art_dir: Path | str | None = None, backend: str | None = None) -> L1Predictor:
        """Lädt ein Artefakt. ``backend`` None → ``metadata.json:winner``."""
        from .lgbm import LgbmL1

        d = Path(art_dir) if art_dir is not None else config.ARTIFACT_DIR
        meta = json.loads((d / "metadata.json").read_text())
        feats = json.loads((d / "features.json").read_text())
        summary = None
        if not meta.get("data_version") and (d / "summary.json").exists():
            summary = json.loads((d / "summary.json").read_text())
        be = backend or meta.get("winner", "lgbm")
        keys = [(t, int(h)) for t in meta["targets"] for h in meta["horizons"]]
        obj = cls(
            art_dir=d, backend=be, meta=meta,
            feature_names=list(feats["feature_names"]),
            raw_inputs=list(feats.get("raw_inputs", [])),
            data_version=artifact_data_version(meta, summary),
        )
        if be == "lgbm":
            obj.lgbm = LgbmL1.load(d / "lgbm", keys)
        elif be == "keras":
            from .keras_model import KerasL1

            obj.keras = KerasL1.load(d / "keras")
        else:
            raise ValueError(f"unbekanntes Backend {be!r}")
        return obj

    @property
    def targets(self) -> list[str]:
        return list(self.meta["targets"])

    @property
    def horizons(self) -> list[int]:
        return [int(h) for h in self.meta["horizons"]]

    def n_model_inputs(self) -> int:
        """Anzahl Eingangsspalten, die das geladene Backend erwartet."""
        if self.backend == "lgbm":
            return int(next(iter(self.lgbm.boosters.values())).num_feature())
        return int(len(self.keras.prep.medians))

    def store_columns(self, targets: list[str] | None = None) -> list[str]:
        """Aus dem Store zu lesende Spalten (für ``feature_names`` nötig + Targets)."""
        cols = list(self.raw_inputs) + required_raw(self.feature_names) + list(targets or self.targets)
        return list(dict.fromkeys(cols))

    def _out(self, t: str, h: int, X: np.ndarray) -> np.ndarray:
        if self.backend == "lgbm":
            return self.lgbm.predict(t, h, X)
        return self.keras.predict(t, h, X)

    def predict_raw(
        self,
        raw: dict[str, np.ndarray],
        ts: np.ndarray,
        targets: list[str] | None = None,
        horizons: list[int] | None = None,
    ) -> dict[tuple[str, int], np.ndarray]:
        """Rohspalten (je Zeile eine Stunde ``t``) → absolute Vorhersagen ``y(t+h)``."""
        X, _ = derive(raw, ts, names=self.feature_names)
        if X.shape[1] != self.n_model_inputs():
            raise ValueError(
                f"Feature-Liste ({X.shape[1]}) passt nicht zum Artefakt ({self.n_model_inputs()} Inputs)"
            )
        out: dict[tuple[str, int], np.ndarray] = {}
        for t in targets or self.targets:
            for h in horizons or self.horizons:
                y = self._out(t, h, X)
                if config.TARGET_KIND[t] == "analog":
                    y = y + np.asarray(raw[t], dtype=np.float64)
                out[(t, h)] = y
        return out

    def predict_at(
        self,
        ts: str,
        features_dir: Path | None = None,
        offset_delta: dict[str, float] | None = None,
        targets: list[str] | None = None,
        check_version: bool = True,
    ) -> dict[int, dict[str, float]]:
        """Vorhersage ab Stunde ``ts`` ('YYYY-MM-DD HH:00') aus dem Feature-Store.

        ``offset_delta`` = Szenario je Zone, z. B. ``{"eg": 2.0}`` (Offset und
        perzipierte Temperatur werden gemeinsam verschoben, v003 zusätzlich der
        Minderer-Regelzustand, s. :func:`m2b.features.apply_offset_step`).
        ``check_version`` = Versionssperre Store ↔ Artefakt (s. :func:`check_data_version`).
        """
        cols = self.store_columns(targets)
        tsa, raw, dv = load_store(features_dir, columns=cols, where=f"ts = TIMESTAMP '{ts}'")
        if check_version:
            check_data_version(dv, self.data_version, str(self.meta.get("artifact", self.art_dir)))
        if len(tsa) != 1:
            raise KeyError(f"Stunde {ts!r} nicht (eindeutig) im Feature-Store")
        for zone, d in (offset_delta or {}).items():
            raw = apply_offset_step(raw, zone, float(d))
        res = self.predict_raw(raw, tsa, targets=targets)
        out: dict[int, dict[str, float]] = {}
        for (t, h), v in res.items():
            out.setdefault(h, {})[t] = float(v[0])
        out[0] = {t: float(raw[t][0]) for t in (targets or self.targets)}
        return dict(sorted(out.items()))


def format_table(res: dict[int, dict[str, float]]) -> str:
    """Kompakte Text-Tabelle (Zeilen = Targets, Spalten = Horizonte)."""
    hs = sorted(res)
    targets = list(res[hs[0]].keys())
    head = f"{'Target':16s}" + "".join(f"{('jetzt' if h == 0 else f'+{h} h'):>10s}" for h in hs)
    lines = [head, "-" * len(head)]
    for t in targets:
        vals = "".join(f"{res[h].get(t, float('nan')):10.2f}" for h in hs)
        lines.append(f"{t:16s}{vals}")
    return "\n".join(lines)
