"""L2 v0.3 config: Empfehlungse-Engine (deterministisch, offline, kein ML).

v0.3 (maintainer-Korrektur 2026-11-02): Komfort wird im **Ist-Raum** gegen das
**abgeleitete RAS-Soll** gemessen (``RAS_BASE_C − Offset``), nicht mehr gegen
die UVR-Standardkurve ``rt_soll(hour)`` (Details:
``docs/l2-v0.3-ist-kalibrierung.md``).

Aufbau (``docs/ai-model-design.md`` §2.2/§3.2, maintainer-Go 2026-11-01):

- **L1 v003** (``models/cmi-brain/v003``, READ-ONLY) liefert die Prognosen:
  ``L1Predictor.predict_at(ts, offset_delta=…)`` pro Horizont (1/4/6/12 h),
  Offset-Aktion über den Horizont gehalten (v002-Ansatz = L2-Fragestellung).
- **Ist-Zustand** = Feature-Store m1.3 (``data/features/``, ``data_version``).
- **Wetter** = Open-Meteo LIVE-24-h-FORECAST (ICON-D2, UTC, s. ``l2.weather``);
  Bias-Korrektur an den CMI-Außensensor (dev0 a8) über das M1-Bias-Profil
  (gleiche Methode wie ``wx_t{k}_t_bias`` im Feature-Store).
- **Aktionsraum (maintainer-Entscheidung 2026-11-01, strikt):**
  Tier A = RAS-Offset je Zone (stündlich, primär); Tier B = regelnbasierte
  Tages-Advisories (Heizkurve/VL-Soll, WW, Zirkulation). EXKLUDIEREN (keine
  Empfehlung): Ölbrenner O5 AN/AUS, Betriebsmodus, Minderer 0–10 V direkt.

Determinismus: kein RNG, Kandidaten-Reihenfolge sortiert (Lexikograf über
``config.ZONES``), Kosten deterministisch aus L1-Output + Zustand.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# --- Pfade (relative zum Repo-Root; ``__file__``-basiert, wie M1/M2b) ---
# parents: [0]=l2, [1]=model, [2]=analysis, [3]=cmi (Repo-Root)
_REPO_ROOT = Path(__file__).resolve().parents[3]
L2_DIR = _REPO_ROOT / "analysis" / "model" / "l2"
# Roh-Cache der LIVE-Forecast-Antworten (API-Antwort 1:1, wie M1 ``wx_cache``).
RUNS_DIR = L2_DIR / "runs"
WX_LIVE_DIR = RUNS_DIR / "wx_live"
REPORT_PATH = L2_DIR / "REPORT.md"

# READ-ONLY (nicht anfassen): Feature-Store m1.3 + L1-Artefakt v003 + M1-Bias-Profil.
FEATURES_DIR = _REPO_ROOT / "data" / "features"
ARTIFACT_DIR = _REPO_ROOT / "models" / "cmi-brain" / "v003"
BIAS_PROFILE_PATH = _REPO_ROOT / "analysis" / "model" / "m1" / "bias_profile.json"

# --- Versionen / Identität (Quellenangabe-DoD, Design §3.2 Regel 6) ---
L1_VERSION = "cmi-brain-v003"
# v0.5 (2026-11-07, maintainer-Beauftragung „VL so niedrig wie möglich"): Ticket D
# = ΔT-Diagnose + pro-Etage-Lag-Schätzung + Konditions-Flag (Fundament für die
# manuellen Minderer/Pumpe/Heizkurve-Empfehlungen der Tickets A/B/C, Plan.md
# §"L2 v0.5").
L2_VERSION = "l2-v005"
# RAS-interner Standard (maintainer 2026-11-02): die RAS-Geräte regeln intern auf
# 20 °C; der Store-Offset bezieht sich immer auf diese 20 (nichts mit den
# 20 °C im UVR-Regler, nichts mit Absenkung — beide „kommen später mit
# rein"). RAS-Soll (Ziel-Raumtemp) = RAS_BASE_C − Offset. EINE Konstante,
# keine je-Zonen-Daten (Q-107: Soll wird nicht als eigener Kanal geloggt).
RAS_BASE_C = 20.0


def model_version() -> str:
    """Kombinierte ``model_version`` für jedes Output-JSON."""
    return f"{L1_VERSION}+{L2_VERSION}"


# --- Standort / Wetter (kanonisch, Q-98; identisch zu M1) ---
LAT = 51.382974
LON = 7.774286
TIMEZONE = "Europe/Berlin"
# Alle Requests in UTC (Open-Meteo ist bei IANA-Zonen DST-unsicher, s. M1-WX_TZ);
# Konvertierung nach Europe/Berlin lokal (``l2.weather.utc_to_berlin_hour``).
WX_TZ = "UTC"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_MODEL = "icon_d2"
HTTP_TIMEOUT_SEC = 30.0
MAX_RETRIES = 2
RETRY_BACKOFF_BASE_SEC = 1.5
USER_AGENT = "cmi-l2-weather/0.1"
# Variablen wie M1 ``WX_HOURLY_VARS`` (gleiche Kurznamen ``m1.config.WX_SHORT``).
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

# --- Stale-Schwellen (Design §3.2 Regel 5: hart, vor Ausgabe) ---
# Metrik-Lücke > 1 h: Lücke = requested − genutzte state-Stunde. Auf dem
# stündlichen Raster sind Lücken ganzzahlig (h) → Schwelle 1,5 h ≙ ≥ 2 h
# fehlende Buckets (steady-state: die laufende Stunde fehlt immer, Lücke
# genau 1 h = normal, NICHT stale).
STORE_STALE_MAX_H = 1.5
# Wetter-Run > 2 h alt => stale (Run = Zeit der letzten Forecast-Ausgabe,
# abgeleitet aus ``generationtime_ms`` der API-Antwort).
WX_RUN_STALE_MAX_H = 2.0
# Forecast-Window, das ein Cache für ``ts`` mindestens abdecken muss:
# Stunden [ts .. ts+23] UTC (Wetter-Fenster 24 h, Design §2.2).
WX_HORIZON_H = 24

# --- Kostenfunktion (Design §3.1/§2.3, v0.1-Defaults; maintainer-einstellbar) ---
# Cost(A, t) = W_COMFORT · Band-Überschreitung (Ist vs. abgeleitetes RAS-Soll
#              = RAS_BASE_C − Offset, v0.3; 12 h)
#            + W_ENERGY  · Energie-Proxy (Ölbrenner-An-Quotient + VL×Pumpenfluss, 12 h)
#            + W_CHANGE  · Σ_Zonen 1[Δ≠0] · 0,5·|ΔK|   (Offset-Änderungs-Strafe)
W_COMFORT = 1.0
W_ENERGY = 0.5
W_CHANGE = 0.3
# No-change-by-default (Design §2.3/§3.2 Regel 1): Kandidat nur, wenn
# Cost-Reduktion gegenüber ``beibehalten`` > Schwellwert.
NO_CHANGE_MIN_DELTA = 0.15
# Energie-Proxy-Skalen (Proxy, Konfidenz-Band — Q-106 Öl-CSV blockiert M7):
# - Öl-Proxy: 1,0 = Brenner 100 % An-Quotient über 12 h (≈ Stunden-Quotient).
# - VL-Proxy: (VL − 20 °C) · Pumpen-An-Quotient, /500 skaliert auf ähnliche
#   Größenordnung (Pumpe bei 60 °C VL läuft selten allein — Mischbetrieb/O9).
VL_REF_C = 20.0
VL_PROXY_SCALE = 500.0
# Unsicherheitsaufschlag 6–12 h (Design §9: Fehler-Kumulierung im Haus-Modell):
# Kosten des +12-h-Wertes (Horizont-Ende) zählen doppelt im Energieterm.
H12_ENERGY_WEIGHT = 2.0
# 6-h-Zwischencheck: der +6-h-Wert zählt zusätzlich (0,5) im Energieterm —
# die erste Hälfte ist die belastbare Hälfte des Rollouts (Design §2.2/§9).
H6_ENERGY_WEIGHT = 0.5

# --- Aktionsraum Tier A: RAS-Offset je Zone (maintainer-Entscheidung 2026-11-01) ---
ZONES: tuple[str, ...] = ("kell", "eg", "dg")
# Kandidaten ΔK um den Ist-Offset (0,5-K-Quantisierung; ±2 K = Guardrail-Max).
OFFSET_CANDIDATES: tuple[float, ...] = (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0)
# Pruning (Raschel-Prävention + plausibles Maß):
# - |Δ| ≤ 1 K pro Zone, wenn ≥ 2 Zonen gleichzeitig nicht 0 sind;
# - sonst |Δ| ≤ 2 K (einzige Zone darf volle Spanne testen).
PRUNE_MULTI_MAX_K = 1.0
PRUNE_SINGLE_MAX_K = 2.0
# Max. Kandidaten (Kappe, deterministisch: sortierte Reihenfolge, Kopf bleibt).
MAX_CANDIDATES = 30
# Guardrail (hart): Offset-Änderung ≤ ±2 K/Stunde.
OFFSET_MAX_DELTA_PER_H = 2.0

# --- Effektor-Guard: Rücklaufminderer 3-h-Fenster (v0.3 Fix-2, 2026-11-03) ---
# ``hzrkl_{z}_rule`` (m1.3) = Offen-Anteil des Rücklaufminderers in Stunde t
# (1 = offen, perzipiert < 20,1 °C, Heizkreis KANN heizen; 0 = zu, perzipiert
# > 20,1, Kreis aus). ``rule_lag1``/``rule_lag2`` = Stunden t−1/t−2 → das
# 3-h-Fenster kommt aus EINER Store-Zeile (kein extra Ladepfad).
# Der RAS kann heizen, aber NICHT kühlen: ist der Minderer seit ~3 h zu
# (Raum wird von externer Wärmequelle, z. B. Trocknungsgerät, aufgeheizt),
# hat der Offset **keine Stellkraft** (höherer Offset kühlt nichts
# Zusätzliches; niedrigerer = falsche Richtung) → Offset-Empfehlung wäre
# „bloß“ (maintainer 2026-11-03): Δ=0 erzwingen + Text-Hinweis. Periodische
# Aktivierung (flappend) außerhalb des Komfort-Bands deutet auf unpassende
# Heizkurve (Q-108) oder Minderer-Einstellung des Heizkreises hin (Advisory,
# KEINE Kandidaten-Untersagung — der Kreis schaltet noch, Offset wirkt
# teilweise). NULL in einer der 3 Stunden → „unbekannt“ → Guard stumm
# (Fail-Open, Empfehlung unverändert).
MINDERER_CLOSED_MAX = 0.1  # 3-h-Ø(open) ≤ 0,1 = „seit 3 h zu“
MINDERER_OPEN_MIN = 0.9    # 3-h-Ø(open) ≥ 0,9 = offen/normal
MINDERER_WINDOW_H = 3     # Fenster-Definition: rule + lag1 + lag2 (t, t−1, t−2)

# --- Guardrails Tier B / Plausibilität (Design §3.2 Regel 4) ---
# Puffer > 70 °C → keine Lade-Empfehlung (Puffer-Überladeschutz).
BUFF_TOP_MAX_C = 70.0
# Kollektor < 25 °C → keine Solar-Hand-EIN (kein Solar-Ertrag).
SOLAR_COLL_MIN_C = 25.0
# Frost-/Puffer-Gate: Puffer < 40 °C + Frost (min. Außen-T 0–12 h ≤ 0 °C)
# → Offsets nur NIE SENKEN (kein Wärmeentzug aus dem Haus, wenn der Puffer leer
# ist und Frost droht).
FROST_BUFF_MIN_C = 40.0
FROST_T_MIN_C = 0.0

# --- Tier B-Schwellen (regelnbasierte Advisories; 0,1-K-Diff-Prinzip) ---
# Heizkurve/VL-Soll: ``vl_err = vlst_est − hz_vl`` (> 0 = Vorlauf unter Soll).
# Empfehlung "Kurven-Absatz −1 Raster (≈ 0,5–1 K)", wenn die Abweichung über
# 12 h im Mittel ≥ 2 K bleibt.
VL_ERR_HIGH_K = 2.0
# Heizbetrieb-Gate (v0.4, 2026-11-03): ``oel_burner_on_sum3h``/
# ``pump_hz_on_sum3h`` (m1.3) = Summe der An-Quotienten der letzten 3 h,
# Bereich 0–3 (1,0 = durchgehend an in 3 h). < EPS ≈ „kein Heizbetrieb“
# (Öl-Brenner + Heizkreispumpe aus) → das VL-Soll hat keine Stellkraft
# (UVR folgt dem VL-Soll nur, wenn geheizt wird) → keine
# Heizkurven-Empfehlung (Ruhezustand, Vorlauf kühlt nur passiv ab).
VL_HEATING_ACTIVE_EPS = 0.05
# WW: ``uvr_ww_margin = 57 − buff_top`` (> 0 = WW-Anforderung aktiv). Solar-
# Vorfüll-Fenster = Strahlungsstunden im nächsten 12 h ≥ WW_SWR_MIN_WH_M2.
WW_SWR_MIN_WH_M2 = 1500.0
# Zirkulation: Nachlauf-Advisory, wenn An-Quotient der letzten 24 h hoch und
# Puffer warm (Zirkulation kann nachlaufen statt Puffer-geführt zu sein).
CIRC_ON_24H_MIN = 12.0
CIRC_BUFF_MIN_C = 50.0

# --- L2 v0.5 Ticket D: ΔT-Diagnose + pro-Etage-Lag (VL-Minimierung) ---
# maintainer-Zielbild (2026-11-07): „Vorlauftemperatur so niedrig wie möglich — nur
# solange mindestens einer der drei Heizkreise Wärme braucht.“ ΔT-Kriterium
# (VL − Strang-RL) ≈ 10 K: **kleiner** ΔT = Wasser fließt zu schnell durch den
# Kreis → Minderer drosseln (V-Ziel = Tickets A). 10 K = the maintainer's
# Geschätzwert, **provisorisch/kalibrierbar** (maintainer 2026-11-07: „noch
# unklar“) → Konstante, fix, von außen konfigurationsfähig (nur hier).
RM_DELTA_T_TARGET_C = 10.0
# „Kreis braucht Wärme“-Kondition: Kreis gilt als aktiv, wenn der
# Minderer-offen-Quotient (befohlene Öffnung, 0–1) über dem Quorum liegt.
# maintainer: Minderer-Feinjustierung macht nur Sinn „ab 2 aktiven Kreisen“ →
# 0,5 = Schwellwert (offen-Quotient ≥ 0,5 über 2 h-Fenster).
RM_ACTIVE_QUOTA = 0.5
# Aktiv-Fenster in Stunden (2-h-Fenster: 2 h-Ø offen-Quotient).
RM_ACTIVE_WINDOW_H = 2
# 3-h-Ø Pumpen-An-Quotient-Schwelle für die Kondition (Pumpe-3h-Ø ≥ EPS).
# Bereich 0–3 (Summe der An-Quotienten der letzten 3 h); 0,5 ≈ „Pumpe in der
# letzten Stunde an“ (1/3 der Zeit).
RM_PUMP_3H_EPS = 0.5
# RAS-Intent-Abweichungsschwelle: perzipiert < Intent − 0,5 K (≥ 1 Zone)
# zwingt die Kondition aktiv (heize aktiv).
RM_RAS_INTENT_GAP_K = 0.5
# --- Lag-Schätzung (10-s-Rohdaten: winsol_pq + cmi_log_pq) ---
# VL→Strang-RL-Lag ist pro Etage unterschiedlich UND durchflussabhängig
# (maintainer, maßgeblich) → direkter 1-h-Vergleich passt selten. Schätzung über
# Kreuzkorrelation (VL[t] vs RL[t+L]) auf den 10-s-Rohdaten. Das Ergebnis ist
# ein **einmaliges, caches-fähiges** Artefakt (``data/l2/rm_lag_estimate.json``),
# NICHT ein pro-recommend()-Neu-Scan der ganzen Historie (aufwendig). Pro-Call
# ΔT = VL/Rohdaten-jetzt mit dem zuletzt geschätzten Lag.
RM_LAG_ESTIMATE_PATH = _REPO_ROOT / "data" / "l2" / "rm_lag_estimate.json"
# Rohdaten-Stores für die Lag-Schätzung (Hive: YYYY/MM/YYYY-MM.parquet, 71 Sp).
RM_RAW_STORES: tuple[str, ...] = ("winsol_pq", "cmi_log_pq")
# Rohkanal-Mapping (verifiziert 2026-11-07, s. docs/cmi-log-protocol.md +
# ui/js/state-map.js preferJson; dev0 = UVR1611 Heizung, dev2 = UVR610
# Rücklaufminderer/Strang-RL):
#   VL (Vorlauf Heizkreis) = dev0 a6; zentraler RL = dev0 a7.
#   Strang-RL je Etage (UVR610 „HzRk Ventil 1“ PT1000):
#       Keller = dev2 a13 (rlK), DG = dev2 a14 (rlDg), EG = dev2 a15 (rlEg).
#       (Zuordnung Keller/DG/EG ↔ ch13/14/15 = state-map.js 2026-11-06.)
#   Minderer-Vorgabe (befohlen, UVR610 „HzRk Ventil 2“, 0–10 V; a10–a12 sind
#       BEFOHLENE Werte, keine Positions-Sensoren, F8):
#       Keller = dev2 a10, EG = dev2 a11, OG/DG = dev2 a12.
#   Pumpe Heizkreis O8 = dev0 d8 (digital 0/1).
RM_RAW_VL = "dev0_analog_06"
RM_RAW_CENTRAL_RL = "dev0_analog_07"
RM_RAW_STRANG_RL: dict[str, str] = {
    "kell": "dev2_analog_13",  # rlK
    "dg": "dev2_analog_14",  # rlDg
    "eg": "dev2_analog_15",  # rlEg
}
RM_RAW_MINDERER: dict[str, str] = {
    "kell": "dev2_analog_10",  # „Keller HzRkl V2“: internes UVR-Signal, pendelt
    # zwischen 0 V (zu) und der Sollspannung (a17/18/19, auf) — Signal „Minderer
    # aktuell auf/zu“ (Aktiv-Semantik), gesetzt von der internen UVR610-Regelung.
    "eg": "dev2_analog_11",
    "dg": "dev2_analog_12",
}
# v0.5-engine-rework (2026-11-12, maintainer): a17/18/19 „Sollspannung 1/2/3“ =
# statische Sollvorgabe/Fixwert je Minderer, von außen (UVR-Programm/externer
# Regler) setzbar — DAS ist die einstellbare Größe (Minderer-Ist-Vorgabe,
# 30-min-Median der Rohdaten; Default 10,0 V bei NULL). Zone-Mapping (Annahme,
# dokumentiert): a17 → kell („Sollspannung 1“), a18 → eg („Sollspannung 2“),
# a19 → dg („Sollspannung 3“, OG). Beleg: app/cmi/cmi_log.py
# ``_CHANNEL_MAP_181`` (json:32 ch17/18/19), Live-JSON (a17–a19 konstant
# 10,0 V seit UVR-Programm-Upload 2026-11-07 16:14; a10–a12 pendeln 0↔10 V).
RM_RAW_MINDERER_SOLL: dict[str, str] = {
    "kell": "dev2_analog_17",  # Sollspannung 1 (json:32 ch17)
    "eg": "dev2_analog_18",  # Sollspannung 2 (json:32 ch18)
    "dg": "dev2_analog_19",  # Sollspannung 3 / OG (json:32 ch19)
}
# Default-Sollspannung, wenn der Rohkanal NULL/fehlend ist (Fixwert, maintainer:
# aktuell konstant 10,0 V je Minderer; v0.6-Pro-Strang-Kennlinie nutzt den
# gemessenen Wert statt dieses Defaults).
RM_MINDERER_SOLL_DEFAULT_V = 10.0
RM_RAW_PUMP = "dev0_digital_08"  # O8 Pumpe Heizkreis
# 0–10 V → 0–1 Offen-Quotient (Minderer-Proxy als Durchfluss-Proxy).
RM_MINDERER_V_SCALE = 10.0
# Schwellen für die Lag-Schätzung:
# - min. 10-s-Samples je Etage für eine **eigene** Lag-Schätzung; darunter
#   (z. B. Keller: Kreis läuft kaum) → Default-Lag-Fallback (n klein).
RM_LAG_MIN_SAMPLES = 500
# - Korrelations-Lag-Suche in Sekunden (0 … max, Schrittbreite 10 s).
RM_LAG_SEARCH_MAX_S = 3600
RM_LAG_STEP_S = 10
# - min. Standardabweichung (°C) der VL- / RL-Serie, damit ein Lag „real“ ist
#   (sonst flaches Signal → Kreuzkorrelation unzuverlässig).
RM_LAG_MIN_STD_C = 0.3
# - min. Samples (nach Lag-Shift) für eine gültige Korrelation.
RM_LAG_MIN_OVERLAP = 100
# Default-Lag (Sekunden), wenn n klein / Kreuzkorrelation unzuverlässig.
# Keller-Kreis läuft kaum → Default-Lag (Keller „zu“, Kreis inaktiv).
RM_DEFAULT_LAG_S = 30
# 2-h-Fenster für pro-Call ΔT (VL-jetzt vs Strang-RL-jetzt, Lag-korrigiert).
RM_DT_WINDOW_H = 2

# --- L2 v0.5 Tickets A/B/C: Minderer / Pumpe / Heizkurve (VL-Minimierung) ---
# Ticket A (Minderer, Advisory): je aktivem Kreis (offen-Ø ≥ ``RM_ACTIVE_QUOTA``
# über ~2 h, Semantik s. ``rm_diagnostics.rm_condition``): ΔT < Ziel → „schließen“
# (V-Ziel = linear aus der ΔT-Fehlmenge: (Ziel−ΔT)/Ziel × aktuelle V, gekappt bei
# 0 V = „max. zu“; **geschätzt** — keine Zwischenpositionen in der Historie,
# binäre 20,1-°C-Ausgabe; V→ΔT-Charakteristik erst ab UVR-Änderung, v0.6),
# ΔT > Ziel → „öffnen“ (in Richtung 10 V, max. 10 V); ΔT im Ziel
# (± ``RM_DT_OK_TOL_K``) → „beibehalten“. **Gate (maintainer-Regel): V-Ziele nur bei
# ≥ 2 aktiven Kreisen**; bei nur 1 aktivem Kreis → Hinweis statt V-Ziel
# (einziger Kreis drosselt nur seinen eigenen ΔT, UVR hebt VL nach).
# Implementierung = statische Offen-Stellung im UVR610-Programm (Default 10 V),
# von außen setzbar → ``advisory-uvr`` (Q-108-Analog, TA-Designer).
RM_DT_OK_TOL_K = 0.5  # ΔT-„beibehalten“-Toleranz (± K, dokumentiert)
RM_V_MAX = 10.0  # Minderer-Offen-Stellung max. (V; F4: 10 V = voll offen)
# Ticket B (Pumpe O8, Advisory): O8 ist CMI-schreibbar nur AUS/EIN/AUTO
# (select 1/2/3, Adresse ``O8_ADDRESS``, INVENTAR 2026-11-02, aktuell AUTO).
# Modus-Empfehlung = AUTO/EIN (AUS wird NIE empfohlen) → ``cmi-settable``
# (S3-1-Pfad, HITL je Change), NUR wenn das Discovery writeable + non-RISK
# bestätigt (sonst Fail-Closed → advisory); Stufung/Druck-Soll =
# ``advisory-manuell`` (Druckregelpumpe: Durchfluss pendelt sich über
# Minderer-Drosselung + Anzahl aktiver Kreise ein; Stufung nur am
# physikalischen Aggregat). Gating: Block nur bei Konditions-Flag aktiv
# (≥ 1 Kreis braucht Wärme), inaktiv → stumm.
O8_ADDRESS = "10820A10180"
O8_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "AUS"),
    ("2", "EIN"),
    ("3", "AUTO"),
)
O8_MODE_REC = "AUTO"  # empfohlener Modus im Heizbetrieb („AUS“ nie)
# v0.5: einzige cmi-setzbare L2-Empfehlungs-Parameter (S3-1, HITL je Change).
# Fail-Closed: ohne gültiges Discovery (writeable + non-RISK) nie cmi-settable.
CMI_SETTABLE_PARAMS: frozenset[str] = frozenset({"pump_o8"})
# Ticket C (Heizkurve, überarbeiteter Tier-B): VL-Soll-Kalibrierung statt
# v0.4 „0,1-K-Diff-Prinzip“ (VL-vs-hz_rl-jetzt methodisch falsch = pro-Etage/
# durchflussabhängiger VL↔RL-Lag, Q-124). Vergleich: **tatsächlicher VL-Ø**
# (VL ist global; Lag-korrektur pro Etage irrelevant hier) × **Heizstunden-Only**
# (Pumpen-An-Quotient ≥ ``VL_KALIB_HEAT_EPS``) × **Außen-T-Bänder (3 K)**
# (``VL_KALIB_BAND_K``) gegen die UVR-Standard-Heizkurve (m2b ``vlst_est`` /
# ``HK_CURVE``) → „Kurve ± n Raster (≈ ±0,5–1 K VL-Soll)“ + „Ziel-VL-Soll bei
# Außen {ext} °C = {y} °C“. Die Band-Kalibrierung ist ein **einmaliges
# Cache-Artefakt** (``VL_KALIB_ESTIMATE_PATH``, CLI ``calibrate-vl``) — pro
# ``recommend()`` nur Cache-Read + Vergleich gegen IST-VL/Außen-T (wie die
# Lags). Cache fehlt → v0.4-Heuristik (Kurve-Absatz) + Hinweis „Kalibrierung
# nicht verfügbar“. v0.4-Guard (kein Heizbetrieb seit 3 h → keine
# Absatz-Empfehlung, Ruhezustand-Text) bleibt für den VL-Block unverändert.
VL_KALIB_ESTIMATE_PATH = _REPO_ROOT / "data" / "l2" / "vl_kalibrierung.json"
VL_KALIB_WINDOW_DAYS = 365  # Heizsaison-Fenster (letztes Jahr inkl. Winter)
VL_KALIB_BAND_K = 3.0  # Außen-T-Bänder (K)
VL_KALIB_MIN_N = 50  # min. Heizstunden pro Band (darunter → Heuristik-Fallback)
VL_KALIB_HEAT_EPS = 0.5  # Heizstunde-Maske: pump_hz_on ≥ 0,5
VL_RASTER_K = 1.0  # 1 Raster ≈ 0,5–1 K VL-Soll; Raster-Zählung konservativ bei 1 K

# --- L2 v0.6-B: L2-Heizkurve v2 — „VL-Minimum s.t. Räume am Soll“ + Recover ---
# maintainer-Zielfunktion (2026-11-12, maßgeblich): „VL so niedrig wie möglich,
# Räume halten trotzdem die Temperatur“ — Pumpen-Laufzeit maximiert ist die
# FOLGE, nicht das Ziel. Zwei Fähigkeiten für den Heizkurven-Block:
# (1) **datenbasiertes VL-Minimum** pro Außen-T-Band: „Wie tief darf die
# Kurve pro Außen-T-Band, bevor ein Raum < Soll − Toleranz fällt?“ — gelernt
# aus Daten (nur ab Lernstart belastbar, s. ``VL_MIN_LEARN_START``);
# (2) **Recover** (live, kein Cache): Raum aktuell ≥ 1,5 K unter Soll →
# Kurven-Absatz N Raster HÖHER (temporär, bis Raum am Soll, dann zurück).
# Die UVR610 regelt die Raum-T selbst aktiv (reaktiv: ABSENK/NORMAL-Aus-
# wertung + RAS) → L2 ergänzt nur antizipativ und zählt das NICHT doppelt
# (Klartext-Hinweis im Advisory).
# Datenmodell (M1-Features-Store, stündliche Buckets): Heizstunde =
# ``pump_hz_on ≥ VL_KALIB_HEAT_EPS`` (Kopie-Referenz: gleiche Maske wie die
# v0.5-Kalibrierung) UND ``ts ≥ VL_MIN_LEARN_START``. Je Zone: Raum-
# Abweichung = ``ras_{z}_ist − (RAS_BASE_C − offset_{z})`` (v0.3-Ableitung,
# identisch zum RAS-Offset-Block in ``recommend._zone_advice``). „Raum am
# Soll“ = Abweichung ≥ −``VL_MIN_AM_SOLL_TOL_K`` (Toleranz, dokumentiert).
# ``vl_min_soll`` pro Band = das ``VL_MIN_QUANTILE``.-Perzentil der
# ``hz_vl``-Werte in den Heizstunden, in denen **alle aktiven Räume** am
# Soll waren (aktiver Raum = RAS-Ist geloggt, ≠ NULL). 95.-Perzentil
# (begründet, dokumentiert): „die Kurve darf so tief, dass in ~95 % der
# Heizstunden mit allen Räumen am Soll der Vorlauf so niedrig oder höher
# war“ = Minimum mit Komfort-Risiko ≤ ~5 % (maintainer: „bevor ein Raum < Soll
# − Toleranz fällt“ → 95 % = konservative Toleranzschwelle für den
# Ausfallanteil).
# Lernstart (v0.6-Markierung, Plan.md): 2026-11-07 16:14 (UVR-Programm-
# Upload; davor: keine statischen Sollspannungen, RAS-Ist/Soll ab P=181
# = 11-07) → davor = nicht belastbar (kein Lernen).
VL_MIN_LEARN_START = "2026-11-07 16:14:00"
VL_MIN_AM_SOLL_TOL_K = 1.5  # „Raum am Soll“-Toleranz (Abweichung ≥ −1,5 K)
VL_MIN_MIN_N = 50  # min. „alle-räume-am-Soll“-Heizstunden pro Band (sonst Fallback)
VL_MIN_QUANTILE = 95  # VL-Perzentil (95.-Perzentil) über den am-Soll-Heizstunden
VL_MIN_ESTIMATE_PATH = _REPO_ROOT / "data" / "l2" / "vl_minimum.json"
# --- Recover (live, kein Cache; maintainer-Zielfunktion: „Raum weit unter Soll →
# VL temporär höher, dann langsam runter“) ---
# Schwellwert: Raum ≥ 1,5 K unter Soll → Kurve N Raster HÖHER (temporär).
# Gleiche Toleranz wie das „am-Soll“-Kriterium (``VL_MIN_AM_SOLL_TOL_K``) —
# eine Konstante für beide Richtungen (konfigurationsfähig, nur hier).
VL_MIN_RECOVER_GAP_K = 1.5  # min. Defizit (K) für die Recover-Empfehlung
VL_MIN_RECOVER_MAX_RASTER = 2  # max. Raster (Kappe; N = min(2, ceil((|dev|−1,5)/1,0)))


def rm_diagnostics_defaults() -> dict[str, dict[str, Any]]:
    """Default-ΔT/Lag-Diagnose (kein Rohdaten-Lag-Schätzungs-Artefakt).

    Fallback, wenn ``data/l2/rm_lag_estimate.json`` fehlt/ungültig: jede Etage
    mit Default-Lag (``RM_DEFAULT_LAG_S``), ``source="default"``,
    ``confidence=0.0``. Deterministisch (kein RNG, keine Datei).
    """
    return {
        z: {
            "lag_s": RM_DEFAULT_LAG_S,
            "confidence": 0.0,
            "source": "default",
            "n": 0,
            "delta_t_c": None,
            "delta_t_target_c": RM_DELTA_T_TARGET_C,
            "flow_class": "unknown",
        }
        for z in ZONES
    }


# --- Konfidenz (aus Modell-Skill des relevanten Targets im v003 summary.json) ---
# Basis-Konfidenz = gewichtete ``skill_b0`` der 12-h-Relevanz-Targets
# (perzipiert RAS + Ölbrenner + Heizkreis-Pumpe), Split 'test', Variante
# ``lgbm_refit`` (= deploytes Artefakt, s. M2b-Pipeline).
CONF_WEIGHTS: dict[str, float] = {
    "ras_kell_perc": 1.0,
    "ras_eg_perc": 1.0,
    "ras_dg_perc": 1.0,
    "oel_burner_on": 0.5,
    "pump_hz_on": 0.5,
}
CONF_FLOOR = 0.10  # untere Klemmung (negativer Skill = Modell < Persistence)
CONF_CAP = 0.95  # obere Klemmung (v1 = Advisory ohne Auto-Write)
CONF_STALE_PENALTY = 0.3  # stale → Konfidenz wird mit diesem Faktor reduziert
CONF_NO_CHANGE = 1.0  # "beibehalten" ist die sichere Basis-Aktion (keine Modell-Wirkung)

# --- Executability (v0.2: discovery-basiert, je-Empfehlung; s.
# ``l2.executability``) ---
# Discovery-Artefakt (CMI-Write-Pfad-Discovery 2026-11-02: 9 Adressen mit
# risk/writeable/option_values; 2026-11-07: +27 Seite-6-Heizkreisregler-
# Parameter, 36 Adressen total, s. INVENTAR.md „Seite 6“). Default = Repo-Pfad (host-seitig, Subagent);
# Override per Env ``CMI_WRITE_INVENTAR`` (Gateway-Container: ro-Mount
# /srv/cmi/scripts/cmi_write_discovery/INVENTAR.json, s. docs/l2-executability.md).
INVENTAR_DEFAULT_PATH = _REPO_ROOT / "scripts" / "cmi_write_discovery" / "INVENTAR.json"

# Executability-Werte (v0.2). Alle ≠ UI-``EXECUTABLE`` (ui/js/ki.js) →
# Badge „Advisory (TA-Designer/v2)“, UI-„Anwenden“ bleibt inaktiv
# (L2 emittiert weiter KEIN ``adresse``/``to_value``). ``EXECUTABILITY_PENDING``
# bleibt als Fail-Closed-Fallback (unbekannte Parameter/kein kuratiertes
# Mapping); bei fehlendem/ungültigem Discovery-Artefakt gibt es **nie**
# ``cmi-settable``.
EXECUTABILITY_PENDING = "pending-cmi-write-discovery"
EXECUTABILITY_RISK_BLOCKED = "risk-blocked"
EXECUTABILITY_CMI_SETTABLE = "cmi-settable"
EXECUTABILITY_ADVISORY_UVR = "advisory-uvr"
EXECUTABILITY_ADVISORY_TA_DESIGNER = "advisory-ta-designer"
EXECUTABILITY_ADVISORY_ZEITPROGRAMM = "advisory-zeitprogramm"
# v0.5 Ticket B: physikalische Stufung/Druck-Soll (nur am Aggregat, nicht
# cmi-setzbar — kein CMI-Kanal) — Advisory-Klasse für den Manuellen-Teil.
EXECUTABILITY_ADVISORY_MANUELL = "advisory-manuell"

# Kuratiertes Mapping: L2-Parameter → (Advisory-Klasse, Q-Bezug). Discovery
# 2026-11-02: RAS-Offsets + Heizkurven-Parameter sind NICHT im
# Schematic-Write-Pfad (9 Adressen) → Advisory (UVR/TA-Designer/Zeitprogramm,
# Q-107/108). Die 7 non-RISK-Whitelist-Schalter sind cmi-setzbar, werden aber
# (bewusst, maintainer-Option 2) NICHT von L2 empfohlen. ``oel_burner_on`` (exkludiert,
# nie empfohlen) dient dem RISK-Schutz: ist der Ölbrenner-Schalter im Discovery
# risk=true → ``risk-blocked`` (niemals setzbar).
ADVISORY_MAP: dict[str, tuple[str, str]] = {
    "offset_kell": (EXECUTABILITY_ADVISORY_UVR, "Q-107 (RAS-Offset-Write, UVR1611-Programmupload/TA-Designer)"),
    "offset_eg": (EXECUTABILITY_ADVISORY_UVR, "Q-107 (RAS-Offset-Write, UVR1611-Programmupload/TA-Designer)"),
    "offset_dg": (EXECUTABILITY_ADVISORY_UVR, "Q-107 (RAS-Offset-Write, UVR1611-Programmupload/TA-Designer)"),
    "vl_curve": (EXECUTABILITY_ADVISORY_TA_DESIGNER, "Q-108 (Heizkurven-Parameter, TA-Designer)"),
    "minderer_kell": (EXECUTABILITY_ADVISORY_UVR, "Q-108-Analog (statische Offen-Stellung im UVR610-Programm, Default 10 V, von außen setzbar — UVR-Programm-Änderung TA-Designer; v0.5-Ticket A)"),
    "minderer_eg": (EXECUTABILITY_ADVISORY_UVR, "Q-108-Analog (statische Offen-Stellung im UVR610-Programm, Default 10 V, von außen setzbar — UVR-Programm-Änderung TA-Designer; v0.5-Ticket A)"),
    "minderer_dg": (EXECUTABILITY_ADVISORY_UVR, "Q-108-Analog (statische Offen-Stellung im UVR610-Programm, Default 10 V, von außen setzbar — UVR-Programm-Änderung TA-Designer; v0.5-Ticket A)"),
    "pump_o8": (EXECUTABILITY_CMI_SETTABLE, f"S3-1 ({O8_ADDRESS}, select AUS/EIN/AUTO; v0.5-Ticket B; Discovery-Verifizierung: writeable + non-RISK, sonst Fail-Closed → advisory)"),
    "pump_o8_stufung": (EXECUTABILITY_ADVISORY_MANUELL, "O8-Druckregelpumpe: Stufung/Druck-Soll NUR am physikalischen Aggregat (kein CMI-Kanal, v0.5-Ticket B)"),
    "ww_window": (EXECUTABILITY_ADVISORY_ZEITPROGRAMM, "Zeitprogramm-Parameter (WW-Ladefenster)"),
    "circulation": (EXECUTABILITY_ADVISORY_UVR, "UVR-Programmier-Aufgabe (nach Nutzungsverhalten statt Puffer-geführt; kein Live-Parameter)"),
    "oel_burner_on": (EXECUTABILITY_ADVISORY_UVR, "Ölbrenner (exkludiert, nie empfohlen; RISK-Schutz: Discovery risk=true)"),
}
# v0.1-kompatible Basis-Hints (bleiben als kuratierte Textquelle; der
# Resolver in ``l2.executability`` ergänzt sie um den Discovery-Bezug).
CMI_HINTS: dict[str, str] = {
    "offset_kell": "Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)",
    "offset_eg": "Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)",
    "offset_dg": "Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)",
    "vl_curve": "TA-Designer-Parameter (Heizkurven-Absatz), v2",
    "ww_window": "Zeitprogramm-Parameter (WW-Ladefenster)",
    "circulation": "UVR-Programmier-Aufgabe (nach Nutzungsverhalten statt Puffer-geführt; kein Live-Parameter)",
}
# Exkludierte Parameter (niemals als Empfehlung, maintainer-Entscheidung 2026-11-01).
EXCLUDED_PARAMETERS: tuple[str, ...] = ("oel_burner_on", "betriebsmodus", "minderer_0_10v")
