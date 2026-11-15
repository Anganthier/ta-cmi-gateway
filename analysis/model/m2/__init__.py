"""M2 — S6-AI-Design: L1 Keras-Anlagen-Modell (1-h-Step, action-conditional).

Trainiert die DYNAMIK der Anlage (S_(t+1) = f(S_t, A_t, W_t, Cal_t)) aus dem
M1-Feature-Store (``data/features/``) und liefert:

- Übergangs-Tabelle (Train ≤ 2024-12-31, Valid 2025, Test 2026) mit
  1-h-ahead-Targets,
- Keras-3-L1 (geteilter Stamm + einer Head pro Target, ≤ 500k Parameter,
  gewichtetes MAE, Early-Stopping auf Valid),
- LightGBM-Pflicht-Vergleich (Q-101), B2-Naive-Baseline (0,5·Persistenz +
  0,5·gleiche Zeit gestern), 4-h-sequenzielles Rollout,
- Offset-Responsivitäts-Test (18.10-Muster: Keller-Offset-Steps, 6-h-Rollout),
- Artefakt ``models/cmi-brain/v001/`` (Keras-Modell, LGBM-Modelle je Target,
  ``scaler.json``, ``metadata.json``, ``README.md``),
- ``REPORT.md`` mit DoD-Checkliste (exakte Zahlen).

Pakete:
- ``m2.config``         Spalten-Definitionen, Gewichte, Seeds, Pfade.
- ``m2.transitions``    Übergangs-Bau (DuckDB → NumPy-Arrays, Train/Valid/Test).
- ``m2.normalize``      StandardScaler (Train-Fit, JSON-serialisierbar).
- ``m2.model``          Keras-L1 (Konstruktion, deterministisches Training,
                        Rollout-Prediction, Offset-Responsivitäts-Pipeline).
- ``m2.lgbm_model``     LightGBM-Referenz (gleiches Feature-Set/Targets).
- ``m2.baseline``       B2-Naive + 4-h-Rollout + Metriken.
- ``m2.responsivity``   Offset-Responsivitäts-Test (Auswahl, ΔH/ΔD, Korrelation).
- ``m2.artifact``       Artefakt-Bau unter ``models/cmi-brain/v001/``.
- ``m2.report``         REPORT.md-Generierung (DoD-Checkliste, Vergleich).
- ``m2.build``          CLI ``python -m analysis.model.m2 build`` (resume-fähig).

Determinismus: Seed 20261031 (``keras.utils.set_random_seed``), TF-
Determinismus (``TF_CUBLAS_DETERMINISTIC=1``, ``oneDNN``-Kernauswahl),
LightGBM ``deterministic=True`` (CPU-Default = bit-reproduzierbar).
GPU-Enablement (Keras): Self-Re-Exec mit ``LD_LIBRARY_PATH`` auf die
nvidia-pip-Bundles (``m2.build.main``) + ``configure_devices``
(Memory-Growth); LGBM-GPU opt-in (``build --lgbm-gpu``, nicht
bit-reproduzierbar garantierbar).
"""

__all__ = [
    "config",
    "transitions",
    "normalize",
    "model",
    "lgbm_model",
    "baseline",
    "responsivity",
    "artifact",
    "report",
    "build",
]
