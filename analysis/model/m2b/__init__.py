"""M2b — L1-Anlagen-Modell v003 (Nachfolger von v002/M2b-m1.2 und M2/v001).

Module:
- ``m2b.config``        Targets, Horizonte, Features, DoD, Hyperparameter.
- ``m2b.data``          Feature-Store m1.3 → Spalten-Arrays, Targets je Horizont (Masken, Purge).
- ``m2b.features``      Feature-Ableitung nach Namen (auch für Szenarien/Responsivität, v002-Namen).
- ``m2b.lgbm``          LightGBM je (Target, h), Monotonie, Refit.
- ``m2b.keras_model``   Keras v2 (skalierte Delta-Targets, maskierter Huber).
- ``m2b.evaluate``      MAE vs. B0/B0-last/B2, DoD, Skill.
- ``m2b.responsivity``  Offset-Responsivität v2.
- ``m2b.pipeline``      Komplett-Lauf → ``models/cmi-brain/v003/`` + REPORT.md.
- ``m2b.predict``       Laden + Vorhersage (Python-API + CLI, Versionssperre Store ↔ Artefakt).

Doku: ``docs/l1-model-v003.md`` (v002: ``docs/l1-model-v002.md``).
CLI: ``python -m analysis.model.m2b --help``.
"""
