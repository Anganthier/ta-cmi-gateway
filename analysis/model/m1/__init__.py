"""M1 — Wetter-Backfill (Open-Meteo) + Feature-Store für das stündliche Heizungs-KI-Modell.

Pakete:
- ``m1.config``          Konstanten, Kanal-Maps (aus ``docs/plan-model.json``), Skemas.
- ``m1.calendar_utils``  Kalender-/Solars-Features (NOAA, holidays DE/NW, DST, Europe/Berlin).
- ``m1.weather``         Open-Meteo-Client (ERA5-Archive + Historical-Forecast), Cache + Fair-Use.
- ``m1.resample``        10-s → 1-h Resampling (DuckDB, Europe/Berlin-Buckets = UTC-Trunk).
- ``m1.lags``            Lag-Berechnung (Kalenderstunden, DST-safe, reines Python).
- ``m1.bias``            Bias-Profil dev0 a8 (CMI-Sensor) vs Grid (saisonal, median + Drift).
- ``m1.features``        Feature-Store-Builder (Hive ``YYYY/MM.parquet``, zstd).
- ``m1.build``           Pipeline-CLI (weather / bias / features / all).
- ``m1.report``          Report-Builder (Coverage, Bias, Spot-Checks → REPORT.md).

Zeitzone: überall Europe/Berlin (Kalenderstunde = UTC-Trunk, da Logger-Wallclock = Berlin).
"""

__all__ = [
    "config",
    "calendar_utils",
    "weather",
    "resample",
    "lags",
    "bias",
    "features",
    "build",
    "report",
]
