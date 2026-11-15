"""L2 v0.2 — Empfehlungse-Engine (deterministisch, Advisory, offline).

L2 liest Ist-Zustand (Feature-Store m1.3) + LIVE-24-h-FORECAST (Open-Meteo
ICON-D2), bewertet Kandidatenaktionen (Tier A: RAS-Offset je Zone, 0,5-K-
Schritte; Tier B: regelnbasierte Tages-Advisories) über einen 12-h-
Entscheidungs-Horizont (6-h-Zwischencheck) gegen L1 v003 und gibt je Stunde
eine Empfehlung als JSON aus — mit deutscher Begründung, Konfidenz, Stale-
Status, model_version, Datenstand und Wetter-Run (Design
``docs/ai-model-design.md`` §3.2).

Module:

- ``l2.config``         Gewichte, Schwellen, Pfade, Kandidaten-Grid, Guardrails,
                        Executability-Mapping (v0.2).
- ``l2.executability``  v0.2: discovery-basierte Executability-Markierung
                        (INVENTAR.json → advisory-Labels / risk-blocked;
                        Fail-Closed, kein ``cmi-settable``).
- ``l2.weather``        LIVE-24-h-Forecast (Open-Meteo, UTC, Bias-Korrektur M1).
- ``l2.recommend``      Die Engine: Zustand → Kandidaten → Rollout → Kosten →
                        Guardrails → Empfehlung (JSON + REPORT.md).
- ``l2.rm_diagnostics`` v0.5 Ticket D: ΔT-Diagnose + pro-Etage-Lag (Kreuz-
                         korrelation auf 10-s-Rohdaten) + Konditions-Flag
                         „≥ 1 Kreis braucht Wärme“ (diagnostisch/advisory,
                         Fundament für Tickets A/B/C + UI).
- ``l2.vl_calibration`` v0.5 Ticket C: VL-Soll-Kalibrierung (Heizstunden-Ø
                          je Außen-T-Band 3 K vs UVR-Standard-Heizkurve;
                          einmaliges Cache-Artefakt, CLI ``calibrate-vl``).

CLI::

    python -m analysis.model.l2 recommend --ts "YYYY-MM-DD HH:MM" [--json]
    python -m analysis.model.l2 estimate-lags [--window-days N] [--now …]
    python -m analysis.model.l2 calibrate-vl [--window-days N] [--now …]

Die Engine: ``from analysis.model.l2.recommend import recommend`` (die
Paket-Importe shadowen den Modulnamen bewusst nicht).

Kein CMI-Zugriff; READ-ONLY: ``analysis/model/m1``, ``m2``, ``m2b``,
``data/features*``, ``models/``.
"""
from __future__ import annotations

from .executability import Discovery, executability_for, load_discovery
from .recommend import Recommendation, save_json, write_report
from .weather import WxLive, fetch_live

__all__ = [
    "Discovery",
    "Recommendation",
    "WxLive",
    "executability_for",
    "fetch_live",
    "load_discovery",
    "save_json",
    "write_report",
]
