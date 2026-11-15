"""M1 config: Konstanten, Pfade, Kanal-Maps und Feature-Schema-Definitionen.

Kanalsemantik ist kanonisch aus ``docs/plant-model.json`` (Feature-Namen abgeleitet,
snake_case). Zeitzone überall Europe/Berlin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

# --- Pfade (relative zum Repo-Root; __file__-basiert, damit Tests + CLI laufen) ---
# parents: [0]=m1, [1]=model, [2]=analysis, [3]=cmi (Repo-Root)
_REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = _REPO_ROOT / "data"
FEATURES_DIR = DATA_DIR / "features"
WX_CACHE_DIR = DATA_DIR / "features" / "wx_cache"
WINSOL_PQ = DATA_DIR / "winsol_pq"
CMI_LOG_PQ = DATA_DIR / "cmi_log_pq"
M1_DIR = _REPO_ROOT / "analysis" / "model" / "m1"
BIAS_PROFILE_PATH = M1_DIR / "bias_profile.json"
REPORT_PATH = M1_DIR / "REPORT.md"
SCHEMA_PATH = M1_DIR / "SCHEMA.md"

# --- Wetter ---
LAT = 51.382974
LON = 7.774286
TIMEZONE = "Europe/Berlin"
# Alle Open-Meteo-Requests laufen in UTC (Open-Meteo ist bei IANA-ZZ DST-unsicher:
# 'Europe/Berlin' liefert konstant GMT+1/CET, auch im Sommer). Konvertierung nach
# Europe/Berlin erfolgt lokal mit zoneinfo.
WX_TZ = "UTC"
ERA5_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HIST_FCST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
SUN_API_URL = "https://archive-api.open-meteo.com/v1/archive"  # daily sunrise/sunset (ERA5)
ERA5_MODEL = "era5"
HFC_MODEL = "icon_d2"
# ICON-D2-Archiv: 2022-11-14 vollständig (2022-11-13 nur 10/24 h, 2022-11-01 null).
# Split: ERA5 bis 2022-11-13, ICON-D2 ab 2022-11-14.
HFC_START = "2022-11-14"
ERA5_BACKFILL_START = "2020-08-01"
ERA5_BACKFILL_END = "2022-11-13"
WX_FETCH_END = "2026-10-31"  # bis Ende der Winsol-/CMI-Daten

# Wetter-Variablen (hourly). shortwave_radiation ist in ERA5 UND ICON-D2 verfügbar.
WX_HOURLY_VARS = [
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "cloudcover",
    "cloudcover_low",
    "cloudcover_mid",
    "cloudcover_high",
    "precipitation",
    "rain",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "weather_code",
]
# Kurze Namen für Feature-Spalten (wx_t{i}_{var}).
WX_SHORT = {
    "temperature_2m": "t",
    "dew_point_2m": "dew",
    "relative_humidity_2m": "rh",
    "cloudcover": "cc",
    "cloudcover_low": "cc_low",
    "cloudcover_mid": "cc_mid",
    "cloudcover_high": "cc_high",
    "precipitation": "prec",
    "rain": "rain",
    "wind_speed_10m": "ws",
    "wind_gusts_10m": "wg",
    "wind_direction_10m": "wd",
    "shortwave_radiation": "swr",
    "weather_code": "wmo",
}
SUN_DAILY_VARS = ["sunrise", "sunset"]

FAIR_USE_SLEEP_SEC = 2.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 2.0
HTTP_TIMEOUT_SEC = 60.0

# --- Feature-Store ---
# m1.1 (M2b-P2): RAS-Maske vor RAS_INSTALL_TS, ras_age_* (Datenalter a11–a13),
# {feat}_last (Stunden-Endwert analog), heat_min_* DEPRECATED (s. SCHEMA.md).
# m1.2: TA-Sentinel-Maske und Rücklaufminderer.
# m1.3: Abgas ×10 + Defektfenster/500-°C-Grenze, UVR-Regelmerkmale und Laufzeitfenster.
DATA_VERSION = "m1.3"
# TA-Sensor-Fehlerkennung (m1.2): UVR1611/UVR610 schreiben bei Fühlerbruch bzw.
# Kurzschluss die Sentinel-Werte 3276.7 bzw. -999.9 in den Analogkanal (gefunden in
# winsol_pq: dev0 a10 Abgas 2021-12-15 → 2022-05-06, dev2 a16 Kollektor 2022-03-25).
# Zentrale Regel: jeder analoge Roh-Wert mit v >= SENTINEL_MAX oder v <= SENTINEL_MIN
# wird bereits in der ``src``-View als NULL behandelt (resample.build_source_view_sql).
# Keine physikalisch plausible Messung der Anlage erreicht diese Grenzen.
SENTINEL_MAX = 3276.0
SENTINEL_MIN = -999.0
# RAS-Einbau (vom Anlagenbesitzer bestätigt, 2026-10-31): die RAS-Raumsensoren wurden
# erst im Oktober 2021 eingebaut. Im m1.0-Store ist der letzte Stunden-Bucket mit
# ras_kell_ist < 5 °C '2021-10-20 15:00', der erste plausible '2021-10-20 17:00'.
# Alle RAS-Rohwerte (RAS_RAW_COLUMNS) mit Roh-ts < RAS_INSTALL_TS sind Müll und werden
# bereits in der ``src``-View als NULL behandelt (resample.build_source_view_sql).
# Format = Roh-ts (Wallclock Europe/Berlin, 'YYYY-MM-DD HH:MM:SS', String-Vergleich).
RAS_INSTALL_TS = "2021-10-20 17:00:00"
# Wetter-Fenster: 24 h (t -> t+23).
WX_WINDOW_H = 24
# Lag-Verschiebungen in Stunden.
LAG_HOURS = [1, 2, 6, 12, 24]
# Forward-Fill-Regel: <= 30 min erlaubt (gap-Flag), sonst Lücke (kein Interpolieren > 1 h).
FF_MAX_MIN = 30
# RAS-Divergenz-Schwelle (dev1 vs dev2, je Zone) in K.
RAS_DIVERGENCE_K = 1.0
# Bias-Korrektur: Grid-T wird bias_korr = grid + (saisonales Median-Offset + Drift).
BIAS_SEASONS = ["DJF", "MAM", "JJA", "SON"]

# --- Kanal-Maps (kanonisch, aus docs/plant-model.json; snake_case-Feature-Namen) ---
# dev0 (UVR1611 DS1, Heizung): analog a1..a16, digital d1..d9 (d1 fehlt im winsol-Schema).
DEV0_ANALOG: dict[int, str] = {
    1: "buff_top",  # Speicher Oben
    2: "buff_mid",  # Speicher Mitte
    3: "buff_bot",  # Speicher Unten
    4: "ww",  # Warmwasser
    5: "oil_vl",  # Oelkessel Vorlauf
    6: "hz_vl",  # Heizkreis Vorlauf
    7: "hz_rl",  # Heizkreis Ruecklauf
    8: "ext",  # Aussen-T (CMI-Sensor, Bias-Referenz)
    9: "wood_vl",  # Holzkessel Vorlauf
    10: "flue",  # Abgastemperatur Holz
    15: "test",  # Test (unused, aber geloggt)
    16: "ww_flow",  # WW-Zirk Stromungsschalter (analog, binär 0/1)
}
DEV0_DIGITAL: dict[int, str] = {
    2: "pump_ww",  # O2 Ladepumpe WW
    3: "mix_open",  # O3 Mischkreis Auf
    4: "mix_close",  # O4 Mischkreis Zu
    5: "oel_burner",  # O5 Anfoerderung Oelbrenner
    6: "pump_ww_oil",  # O6 Ladep.WW.5 (WW vom Oel)
    7: "pump_circ",  # O7 Zirkulationspumpe
    8: "pump_hz",  # O8 Pumpe Heizkreis
    9: "valve_hzrl",  # O9 Ventil Heizungsruecklauf
}
# dev1 (UVR1611 DS2, RAS): analog a1..a13 (a3/a6/a9/a10/14/15/16 unused), digital d1..d13.
DEV1_ANALOG: dict[int, str] = {
    1: "ras_kell_perc",  # Perzipiert Keller (= Ist + Offset)
    2: "ras_kell_ist",  # Ist Keller
    4: "ras_eg_perc",  # Perzipiert EG
    5: "ras_eg_ist",  # Ist EG
    7: "ras_dg_perc",  # Perzipiert DG
    8: "ras_dg_ist",  # Ist DG
    # a11–a13 = Datenalter ("Zeit seit letzter Datenübertragung der RAS-Elemente"),
    # KEINE Heiz-Minuten: Sägezahn +0,1 je 60 s, Reset auf 0 nach ≈1,1 (≈11 min),
    # steigt bei Funkausfall weiter (99,9-Perzentil Stunden-Max ≈ 250).
    11: "ras_age_kell",  # Datenalter RAS Keller
    12: "ras_age_eg",  # Datenalter RAS EG
    13: "ras_age_dg",  # Datenalter RAS DG
}
# DEPRECATED (m1.1): ``heat_min_*`` = Stunden-Summe der positiven Deltas von a11–a13.
# KEINE Heiz-Minuten (Sägezahn → konstruktionsbedingt ≈ 6 je Stunde = informationslos);
# nur für v001-Kompatibilität im Store, NICHT als Feature/Target verwenden.
HEAT_MIN_DEPRECATED: list[tuple[int, str]] = [
    (11, "heat_min_kell"),
    (12, "heat_min_eg"),
    (13, "heat_min_dg"),
]
DEV1_DIGITAL: dict[int, str] = {}  # ungenutzt (Setup: 'DIGITAL 1..13 unbenutzt')
# dev2 (UVR610, Solar+RAS): analog a1..a16, digital d1 (Solarpumpe).
DEV2_ANALOG: dict[int, str] = {
    1: "solar_tank_top",
    2: "solar_tank_mid",
    3: "solar_tank_bot",
    4: "ras2_kell_ras",  # RAS Keller (CAN-Transfer, nur Konsistenz-Check)
    5: "ras2_kell_ist",  # Temp Keller
    6: "ras2_eg_ras",
    7: "ras2_eg_ist",
    8: "ras2_dg_ras",
    9: "ras2_dg_ist",
    # a13–a15 = Rücklaufminderer "HzRkl Ventil 1" je Etage (m1.2), stetig ≈ 11–65,
    # Rohwert-Einheit laut Setup %. ACHTUNG Reihenfolge: a14 = OG (→ dg), a15 = EG.
    13: "hzrkl_kell_pos",  # Keller HzRkl Ventil 1
    14: "hzrkl_dg_pos",  # OG HzRkl Ventil 1
    15: "hzrkl_eg_pos",  # EG HzRkl Ventil 1
    16: "solar_coll",  # Kollektor
}
# dev2 a10–a12 = Rücklaufminderer "HzRkl Ventil 2 Ergebnis" je Etage (m1.2). Rohwert
# binär 0 / 10 (Messbefund ab 2022-03-06) → Features ``hzrkl_{z}_on`` (Stunden-AVG von
# Rohwert / HZRKL_ON_SCALE = An-Quotient 0..1 wie die Digital-``_on``) und
# ``hzrkl_{z}_last`` (letzter Nicht-NULL-Rohwert / HZRKL_ON_SCALE). Bewusst NICHT in
# DEV2_ANALOG (kein Roh-AVG unter dem Basisnamen).
DEV2_HZRKL_ON: dict[int, str] = {
    10: "hzrkl_kell",  # Keller HzRkl Ventil 2 Ergebnis
    11: "hzrkl_eg",  # EG HzRkl Ventil 2 Ergebnis
    12: "hzrkl_dg",  # OG HzRkl Ventil 2 Ergebnis
}
HZRKL_ON_SCALE = 10.0
HZRKL_ZONES: list[str] = ["kell", "eg", "dg"]
# UVR610-Mindererregel (Q-110): unter 20,1 °C offen, darüber zu, genau 20,1 °C hält.
HZRKL_RULE_T_C = 20.1
# dev0 a10 wird mit Faktor 10 in echte °C überführt. Das Defektfenster ist der
# nachgewiesene Kurzschlusszeitraum; 450–500 °C außerhalb davon bleiben gültig.
FLUE_SCALE = 10.0
FLUE_DEFECT_START = "2022-04-28 10:00:00"
FLUE_DEFECT_END = "2022-05-06 14:00:00"
FLUE_MAX_C = 500.0
# O1-Holzpumpe fehlt im Logging; Schaltregel aus docs/uvr-programm-analyse.md §4.
WOOD_PUMP_MIN_VL_C = 65.0
WOOD_PUMP_MIN_DIFF_K = 3.0
WOOD_PUMP_MAX_BUFFER_C = 90.0
WOOD_FIRE_MIN_FLUE_C = 100.0
# Laufzeit-/Aktivitätsfenster aus stündlichen An-Quotienten bzw. markierten Proxies.
RUNTIME_COLUMNS: tuple[str, ...] = (
    "oel_burner_on",
    "pump_wood_est_on",
    "wood_fire_on",
    "pump_ww_on",
    "pump_ww_oil_on",
    "pump_circ_on",
    "pump_hz_on",
    "pump_solar_on",
    "valve_hzrl_on",
)
RUNTIME_WINDOWS: tuple[int, ...] = (3, 6, 12, 24)
SINCE_COLUMNS: tuple[str, ...] = RUNTIME_COLUMNS
DEV2_DIGITAL: dict[int, str] = {
    1: "pump_solar",  # Solarpumpe
}

# Feature-Zonen für RAS-Offset (Δ = perc - ist).
RAS_ZONES: list[tuple[str, str, str]] = [
    # (perc_col, ist_col, zone_key)
    ("ras_kell_perc", "ras_kell_ist", "kell"),
    ("ras_eg_perc", "ras_eg_ist", "eg"),
    ("ras_dg_perc", "ras_dg_ist", "dg"),
]
# dev2-RAS-Zonen für Divergenz-Flag (dev1 ist vs dev2 ist).
RAS2_ZONES: list[tuple[str, str]] = [
    # (dev1_ist_col, dev2_ist_col)
    ("ras_kell_ist", "ras2_kell_ist"),
    ("ras_eg_ist", "ras2_eg_ist"),
    ("ras_dg_ist", "ras2_dg_ist"),
]


def dev_col(device: int, kind: str, ch: int) -> str:
    """Roh-Parquet-Spaltenname (winsol/cmi): dev{N}_{analog|digital}_{MM}."""
    return f"dev{device}_{kind}_{ch:02d}"


# RAS-Roh-Kanäle (Maske vor RAS_INSTALL_TS): dev1 a1,a2,a4,a5,a7,a8 (perc/ist) +
# a11–a13 (Datenalter) sowie die dev2-Kanäle, die auf ``ras2_*`` gemappt sind (a4–a9).
RAS_RAW_COLUMNS: list[str] = [dev_col(1, "analog", ch) for ch in sorted(DEV1_ANALOG)] + [
    dev_col(2, "analog", ch) for ch, feat in sorted(DEV2_ANALOG.items()) if feat.startswith("ras2_")
]

# Analoge Features mit zusätzlichem ``{feat}_last`` = letzter Nicht-NULL-10-s-Wert der
# Stunde (Stunden-Endwert; ergänzt den Stunden-Mean).
LAST_VALUE_FEATURES: list[str] = [
    "ras_kell_perc",
    "ras_kell_ist",
    "ras_eg_perc",
    "ras_eg_ist",
    "ras_dg_perc",
    "ras_dg_ist",
    "buff_top",
    "buff_mid",
    "buff_bot",
    "hz_vl",
    "hz_rl",
    "ww",
    "ext",
    "oil_vl",
    "wood_vl",
    "flue",
    "solar_coll",
    # m1.2: Rücklaufminderer-Ventilstellung (a13/a15/a14) → hzrkl_{z}_pos_last
    "hzrkl_kell_pos",
    "hzrkl_eg_pos",
    "hzrkl_dg_pos",
]


def analog_raw_col(feat: str) -> str:
    """Roh-Spaltenname eines analogen Features (Umkehrung der DEV*_ANALOG-Maps).

    Raises ``KeyError`` bei unbekanntem Feature-Namen.
    """
    for dev, mapping in ((0, DEV0_ANALOG), (1, DEV1_ANALOG), (2, DEV2_ANALOG)):
        for ch, name in mapping.items():
            if name == feat:
                return dev_col(dev, "analog", ch)
    raise KeyError(feat)


def col_for(device: int, kind: str, ch: int) -> str | None:
    """Feature-Name (snake_case) für ein Roh-Kanal; None wenn unbenannt/unused
    oder unbekanntes Device/Kanal."""
    tables = {
        ("analog", 0): DEV0_ANALOG,
        ("analog", 1): DEV1_ANALOG,
        ("analog", 2): DEV2_ANALOG,
        ("digital", 0): DEV0_DIGITAL,
        ("digital", 1): DEV1_DIGITAL,
        ("digital", 2): DEV2_DIGITAL,
    }
    m = tables.get((kind, device))
    if m is None:
        return None
    return m.get(ch)


@dataclass
class BuildConfig:
    """Laufzeit-Konfiguration für die Pipeline (pfad-/zeit-variabel, Default = Produktion).

    ``first_hour`` = erste volle Kalenderstunde des Feature-Rasters (Europe/Berlin).
    Produktion: 2020-08-31 00:00 (Logger-Start, s. M1-Spec). Tests dürfen das
    Raster kleiner wählen (synthetische Quellen).
    """

    features_dir: Path = FEATURES_DIR
    wx_cache_dir: Path = WX_CACHE_DIR
    winsol_pq: Path = WINSOL_PQ
    cmi_log_pq: Path = CMI_LOG_PQ
    bias_profile: Path = BIAS_PROFILE_PATH
    data_version: str = DATA_VERSION
    fetch_end: str = WX_FETCH_END
    fair_use_sleep: float = FAIR_USE_SLEEP_SEC
    first_hour: datetime = field(
        default_factory=lambda: datetime(2020, 8, 31, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    )
    extra: dict = field(default_factory=dict)


def month_range(start: str, end: str) -> list[tuple[int, int]]:
    """(year, month)-Paare von start bis end (inklusiv)."""

    y0, m0 = int(start[:4]), int(start[5:7])
    y1, m1 = int(end[:4]), int(end[5:7])
    out: list[tuple[int, int]] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


# --- DuckDB-RAM-Budget (R4'-M1-OOM-Gegenmaßnahme, maintainer-Mandat 2026-11-03) ------
# M1 läuft auf dem 4-GB-Pi (ohne Swapfile, neben ~2,5 GB Baseline) → das
# harte RAM-Budget für M1-DuckDB ist 500 MB. 19,2-Mio-Row-Scans laufen daher
# monats-chunked (features.store_features: ein Kalendermonat pro
# ``build_hourly_sql``, Default 4 Threads), nie als monolithischer Vollspan-
# Output (gemessen: OOM bei 477,0/476,8 MiB; Peak ~21 GB Referenz-Host).
# Defaults: memory_limit 500 MB, 4 Threads (Pi = 4× Cortex-A72; Referenz-Host
# 20 Kerne, für die Benchmarks via M1_DUCKDB_THREADS=20 anhebbar).
DUCKDB_MEMORY_LIMIT_DEFAULT = "500MB"
DUCKDB_THREADS_DEFAULT = 4


def con() -> duckdb.DuckDBPyConnection:
    """Frische in-memory-Verbindung (Read-only-Quellen via read_parquet).

    RAM-Budget: ``memory_limit`` = 500 MB (Default) + ``threads`` = 4 (Default),
    beide per Env-Override: ``M1_DUCKDB_MEMORY_LIMIT`` (z. B. ``"500MB"``,
    ``"1GB"``) und ``M1_DUCKDB_THREADS`` (int). 500 MB ist das harte
    RAM-Budget für M1 auf dem 4-GB-Pi (maintainer-Mandat, R4'); der Schutz gegen
    Host-OOM ist ausschließlich ``memory_limit`` + Chunking (kein Swapfile).
    Beleg DuckDB 2.0.0: ``SET memory_limit='500MB'`` →
    ``current_setting('memory_limit')`` = ``'476.8 MiB'``.
    """
    mem = os.environ.get("M1_DUCKDB_MEMORY_LIMIT", DUCKDB_MEMORY_LIMIT_DEFAULT)
    threads = os.environ.get("M1_DUCKDB_THREADS", str(DUCKDB_THREADS_DEFAULT))
    return duckdb.connect(config={"memory_limit": mem, "threads": threads})
