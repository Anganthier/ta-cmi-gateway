"""M2 — StandardScaler (Train-Fit, JSON-serialisierbar).

Einfacher Standard-Scaler (mean/std) für **alle** Features (State, Lags,
Aktion, Wetter, Kalender, Flags, NULL-Flag-Spalten). Begründung: z-score ist
reproduzierbar, framework-neutral (Keras **und** LightGBM teilen denselben
Scaler), und die Features sind (bis auf wenige Ausreißer) ungefähr
unimodal — robustes Quantil-Scaling würde die für GBDTs irrelevante
Monotonität kosten, und Keras profitiert von Standardisierung.

Regel: **fit nur auf Train** (walk-forward: kein Look-Ahead von Valid/Test in
die Normalisierung). ``std`` wird als ``sqrt(var + 1e-8)`` gesetzt.

**Bugfix M2b-P0 (2026-10-31):** Im Train *konstante* Spalten (z. B.
``mode_known`` und die meisten NULL-Flag-Spalten, die im Train immer 0 sind)
bekamen früher ``std = 1e-4``. Wurde ein solches Flag in Valid/Test doch 1,
entstand ``z = 10.000`` → Keras-Ausreißer > 9000 K (11 Test-Stunden). Jetzt:

- konstante Spalten werden nur zentriert (effektive ``std = 1``; gilt auch
  für alte ``scaler.json`` mit gespeichertem ``std = 1e-4`` → v001-kompatibel),
- ``transform`` kappt z-Werte standardmäßig auf ``±CLIP_Z`` (10), damit
  ungesehene Extremwerte das Netz nicht in einen unbekannten Bereich treiben.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_EPS = 1e-8
_STD_MIN = 1e-4
# z-Kappung im transform (M2b-P0): ±10 σ deckt > 99,9 % aller Train-Werte ab.
CLIP_Z = 10.0


def _effective_std(std: np.ndarray) -> np.ndarray:
    """Konstante Spalten (gespeichertes std ≤ 1e-4) → 1.0 (nur zentrieren)."""
    std = np.asarray(std, dtype=np.float64)
    return np.where(std <= _STD_MIN * 1.001, 1.0, std)


@dataclass
class StandardScaler:
    """Zweiseitig (fit/transform) skalierter Scaler für 2D-Float-Arrays."""

    mean_: np.ndarray
    std_: np.ndarray
    n_samples_seen: int = 0
    n_features: int = 0

    @classmethod
    def fit(cls, X: np.ndarray) -> StandardScaler:
        """Fittet mean/std auf ``X`` (Shape (n, d))."""
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2:
            raise ValueError(f"erwartet 2D, bekam {X.shape}")
        mean = X.mean(axis=0)
        std = np.sqrt(X.var(axis=0) + _EPS)
        std = np.maximum(std, _STD_MIN)
        std = _effective_std(std)  # konstante Spalten: std = 1 (Bugfix M2b-P0)
        return cls(mean_=mean.astype(np.float32), std_=std.astype(np.float32), n_samples_seen=int(X.shape[0]), n_features=int(X.shape[1]))

    def transform(self, X: np.ndarray, clip: float | None = CLIP_Z) -> np.ndarray:
        """Skaliert ``X`` (Shape (n, d)) mit den gefitteten Parametern.

        ``clip``: z-Werte auf ``±clip`` kappen (Default ``CLIP_Z``; ``None`` =
        ungekappt, z. B. für exakte Round-Trips mit Ausreißern).
        """
        X = np.asarray(X)
        if X.ndim == 1:
            X = X[None, :]
        if X.shape[1] != self.n_features:
            raise ValueError(
                f"n_features passt nicht: erwartet {self.n_features}, bekam {X.shape[1]}"
            )
        z = (np.asarray(X, dtype=np.float64) - self.mean_) / _effective_std(self.std_)
        if clip is not None:
            z = np.clip(z, -float(clip), float(clip))
        return z.astype(np.float32)

    def inverse_transform(self, X: np.ndarray) -> np.ndarray:
        """Rücktransformierung (Round-Trip-Prüfung)."""
        X = np.asarray(X)
        if X.ndim == 1:
            X = X[None, :]
        if X.shape[1] != self.n_features:
            raise ValueError(
                f"n_features passt nicht: erwartet {self.n_features}, bekam {X.shape[1]}"
            )
        return (np.asarray(X, dtype=np.float64) * _effective_std(self.std_) + self.mean_).astype(np.float32)

    def roundtrip_residual(self, X: np.ndarray) -> float:
        """Max-Abweichung ``|inverse(transform(X)) - X`` (Prüf-Metrik)."""
        return float(np.max(np.abs(self.inverse_transform(self.transform(X, clip=None)) - X.astype(np.float64))))

    # --- Serialisierung ---
    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "standard_scaler",
            "n_features": int(self.n_features),
            "n_samples_seen": int(self.n_samples_seen),
            "mean": [float(v) for v in self.mean_],
            "std": [float(v) for v in self.std_],
        }

    def save_json(self, path: str | object) -> None:
        import json
        from pathlib import Path

        p = Path(str(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> StandardScaler:
        if d.get("kind") != "standard_scaler":
            raise ValueError(f"kein StandardScaler: {d.get('kind')}")
        return cls(
            mean_=np.asarray(d["mean"], dtype=np.float32),
            std_=np.asarray(d["std"], dtype=np.float32),
            n_samples_seen=int(d.get("n_samples_seen", 0)),
            n_features=int(d["n_features"]),
        )

    @classmethod
    def load_json(cls, path: str | object) -> StandardScaler:
        import json
        from pathlib import Path

        return cls.from_dict(json.loads(Path(str(path)).read_text()))
