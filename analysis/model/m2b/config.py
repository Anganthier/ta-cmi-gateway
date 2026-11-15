"""M2b config: L1-Modell v003 (Delta-Targets, direkte Horizonte, Store m1.3).

v003 (Begründung + Messwerte: ``docs/l1-model-v003.md``) baut auf v002 auf:

- **Daten m1.3:** ``flue``/``flue_last`` in echten °C (Rohwert ×10, Defektfenster
  und Werte > 500 °C NULL), Minderer-Regel ``hzrkl_{z}_rule*``, Laufzeit-Fenster
  ``{sig}_sum{w}h``/``{sig}_since_h``, Holz-Proxys, O9-Marge (M1-SCHEMA §2.14, §9).
- **Live-Lücke:** gemessene ``hzrkl_{z}_on``/``_last`` (+ Lags) sind seit
  2026-10-27 im CMI-Live-Log NULL → als Input durch die Regel ersetzt, als
  Target (Stellglied der Responsivität) beibehalten.
- v002 bleibt über ``--model models/cmi-brain/v002 --features-dir data/features_m1.2``
  nutzbar (Namens-basierte Feature-Ableitung, Versionssperre im Predictor).

Leitidee v002 (``docs/l1-model-v002.md``):

- **Delta statt Level:** Analog-Targets werden als Änderung
  ``y(t+h) − y(t)`` gelernt (RAS-Level-std 8–9 K vs. 1-h-Δ-std ≈ 0,17 K).
  Digital-Targets (An-Quotient 0..1) bleiben Level (``cross_entropy``).
- **Direkt statt Rollout:** je Horizont ``h`` ein eigenes Modell. Kein
  Fehler-Kumulieren, keine eingefrorenen Lags/Wetterfenster. Die Aktion der
  Stunde ``t`` (Digital-Schalter, RAS-Offsets) gilt als über ``h`` gehalten —
  genau die Frage, die L2 stellt („was passiert, wenn ich jetzt X setze?“).
- **B0 Persistence ist Pflicht-Baseline** (DoD), B2 nur noch informativ.
- **Daten m1.1:** RAS vor Einbau (Okt. 2021) = NULL, ``ras_age_*`` statt
  ``heat_min_*`` (a11–a13 = Datenalter der RAS-Funkübertragung, s. CIR-Log),
  ``*_last`` = letzter 10-s-Wert der Stunde.

Spaltennamen kanonisch aus ``analysis/model/m1/SCHEMA.md``.
"""
from __future__ import annotations

import re
from pathlib import Path

from analysis.model.m2 import config as m2c

# --- Pfade ---
_REPO_ROOT = Path(__file__).resolve().parents[3]
FEATURES_DIR = _REPO_ROOT / "data" / "features"
M2B_DIR = _REPO_ROOT / "analysis" / "model" / "m2b"
MODELS_DIR = _REPO_ROOT / "models"
ARTIFACT_NAME = "cmi-brain-v003"
ARTIFACT_DIR = MODELS_DIR / "cmi-brain" / "v003"
ARTIFACT_V002_DIR = MODELS_DIR / "cmi-brain" / "v002"
ARTIFACT_V001_DIR = MODELS_DIR / "cmi-brain" / "v001"
REPORT_PATH = M2B_DIR / "REPORT.md"
RUNS_DIR = M2B_DIR / "runs"

# --- Version / Seed / Splits (walk-forward, identisch zu M2) ---
EXPECTED_DATA_VERSION = "m1.3"
# Artefakte ohne ``data_version`` in metadata/summary (Annahme: v002-Ära).
ARTIFACT_DATA_VERSION_DEFAULT = "m1.2"
# Passender Feature-Store je data_version (Hinweis in der Versionssperre des Predictors).
FEATURES_DIR_HINT: dict[str, str] = {
    "m1.0": "data/features_m1.0",
    "m1.1": "data/features_m1.1",
    "m1.2": "data/features_m1.2",
    "m1.3": "data/features",
}
# flue ist ab m1.3 in °C (Rohwert ×10): Faktor für Vergleiche mit v002-Metriken.
V002_SCALE: dict[str, float] = {"flue": 10.0}
SEED = m2c.SEED
VALID_START = m2c.VALID_START  # 2025-01-01
TEST_START = m2c.TEST_START  # 2026-01-01
# Optionaler Trainingsbeginn (Stunden davor = Split-Label 'pre', ungenutzt).
# Hypothese: Regime-Wechsel 2021-10-20 (RAS-Einbau + neues UVR1611-Programm
# `20211020_owner.tdw`; Heizkreis-Pumpe 2020/21 75–91 % an, 2022–26 33–50 %).
# Experiment 2026-11-01 (docs/l1-model-v002.md §4.4): Start 2021-10-20 ändert RAS
# nicht (vorher ohnehin NULL), Puffer ±1 %, pump_hz_on +4 h RMSE 0,346 → 0,366
# (schlechter) → **Default None = alle Daten**; RAS-Targets sind über die
# m1.1-Maske trotzdem nur ab Einbau trainiert. CLI: ``--train-start "2021-10-20 17:00"``.
TRAIN_START: str | None = None

# --- Horizonte (direkte Modelle) ---
HORIZONS: tuple[int, ...] = (1, 4, 6, 12)
DOD_HORIZON = 4

# --- Targets ---
RAS_IST_TARGETS: tuple[str, ...] = ("ras_kell_ist", "ras_eg_ist", "ras_dg_ist")
RAS_PERC_TARGETS: tuple[str, ...] = ("ras_kell_perc", "ras_eg_perc", "ras_dg_perc")
RAS_TARGETS: tuple[str, ...] = RAS_IST_TARGETS + RAS_PERC_TARGETS
BUFF_TARGETS: tuple[str, ...] = ("buff_top", "buff_mid", "buff_bot")
ZONES: tuple[str, ...] = ("kell", "eg", "dg")
# Rücklaufminderer je Etage (UVR610 dev2, m1.2; Q-110 maintainer 2026-11-01: je Etage
# eigener Minderer 0–100 % des Grundbedarfs, eine gemeinsame Pumpe):
#   hzrkl_{z}_on   = „HzRkl Ventil 2 Ergebnis“ (a10–a12, binär) als An-Quotient 0..1 =
#                    Stellglied, über das ein Etagen-Offset wirkt (perzipiert ↑ ⇒ Ventil zu).
#                    v003: nur noch Target (live seit 2026-10-27 NULL), Input = Regel.
#   hzrkl_{z}_pos  = „HzRkl Ventil 1“ (a13–a15) = **Temperatur am Minderer in °C**
#                    (M1-SCHEMA §2.13), keine Ventilstellung; der Spaltenname ist historisch.
#                    Kommt live weiter an → Input + physikalisches Analog-Target ohne
#                    Regler-Sicht/Monotonie.
#   hzrkl_{z}_rule = Minderer-Zustand aus der UVR610-Regel (offen < 20,1 °C perzipiert,
#                    zu > 20,1 °C, sonst halten; m1.3, SCHEMA §2.14): Live-Ersatz für _on.
HZRKL_ON_TARGETS: tuple[str, ...] = tuple(f"hzrkl_{z}_on" for z in ZONES)
HZRKL_POS_TARGETS: tuple[str, ...] = tuple(f"hzrkl_{z}_pos" for z in ZONES)
ANALOG_TARGETS: tuple[str, ...] = RAS_TARGETS + (
    "hz_vl",
    "hz_rl",
) + BUFF_TARGETS + (
    "ww",
    "solar_coll",
    "wood_vl",
    "flue",
    "oil_vl",
) + HZRKL_POS_TARGETS
# Offsets sind Aktion (über h gehalten) → kein Target in v002 (v001: Target).
DIGITAL_TARGETS: tuple[str, ...] = m2c.DIGITAL_TARGETS + HZRKL_ON_TARGETS
TARGETS: tuple[str, ...] = ANALOG_TARGETS + DIGITAL_TARGETS

ZONE_OF_RAS: dict[str, str] = {f"ras_{z}_{k}": z for z in ZONES for k in ("ist", "perc")}
ZONE_OF_HZRKL: dict[str, str] = {f"hzrkl_{z}_{k}": z for z in ZONES for k in ("on", "pos")}

# Minderer-Regel (UVR610, SCHEMA §2.14): offen (1) wenn perzipiert < Schwelle, zu (0)
# darüber, bei Gleichstand (|Δ| < EPS, RAS-Auflösung 0,1 K) oder NULL Zustand halten.
HZRKL_RULE_THRESHOLD = 20.1
HZRKL_RULE_EPS = 1e-6

# --- UVR-Schaltschwellen (docs/uvr-programm-analyse.md §2.2/§2.3, gegengeprüft 2026-11-01) ---
UVR_O9_DIFF_K = 5.0  # O9 Ventil HzRL an: a2 Puffer-Mitte > a7 Heizkreis-RL + 5 K (aus < 3 K)
UVR_O2_DIFF_K = 24.6  # O2 Öl-Ladepumpe an: a5 Öl-VL > a1 Puffer-Oben + 24,6 K (aus < 22,6 K)
UVR_WW_SOLL = 57.0  # AnfWW → O5: a1 Puffer-Oben < 57 °C im Zeitfenster (aus bei +4 K)
UVR_O1_MIN = 65.0  # O1 Holz-Ladepumpe: a9 Holz-VL > 65 °C …
UVR_O1_DIFF_K = 3.0  # … und a9 > a3 Puffer-Unten + 3 K (und a3 < 90 °C)
# Heizkurve u1611FunkHeizkreis: +10 °C → VL 45 °C, −20 °C → VL 65 °C, VLMin 25, VLMax 70.
# Raumeinfluss (70 %) und Steilheit 60 sind in der Schätzung nicht modelliert.
HK_CURVE: tuple[tuple[float, float], tuple[float, float]] = ((10.0, 45.0), (-20.0, 65.0))
HK_VL_MIN = 25.0
HK_VL_MAX = 70.0

# --- Datenqualität RAS ---
# ras_age = Stunden-MAX des a11–a13-Sägezahns (0,1 je Minute ⇒ Wert×10 ≈ min
# seit letzter Funkübertragung). Normal: Reset nach ≈1,1 (≈11 min).
# > RAS_STALE_AGE ⇒ RAS-Wert der Stunde gilt als eingefroren → Target maskiert.
RAS_STALE_AGE = 3.0

# --- Inputs ---
# Basis = M2-Featureliste ohne heat_min_* (informationslos) + m1.1-Spalten.
_DROP_BASE = {"heat_min_kell", "heat_min_eg", "heat_min_dg", "mode_known"}
BASE_INPUTS: tuple[str, ...] = tuple(
    c
    for c in m2c.FEATURE_INPUTS
    if c not in _DROP_BASE and not c.startswith("heat_min_")
)
LAST_COLS: tuple[str, ...] = (
    "ras_kell_perc", "ras_kell_ist", "ras_eg_perc", "ras_eg_ist",
    "ras_dg_perc", "ras_dg_ist", "buff_top", "buff_mid", "buff_bot",
    "hz_vl", "hz_rl", "ww", "ext", "oil_vl", "wood_vl", "flue", "solar_coll",
)
LAST_INPUTS: tuple[str, ...] = tuple(f"{c}_last" for c in LAST_COLS)
AGE_INPUTS: tuple[str, ...] = tuple(f"ras_age_{z}" for z in ZONES)
_LAGS: tuple[int, ...] = (1, 2, 6, 12, 24)  # = M1 LAG_HOURS
HZRKL_RULE_COLS: tuple[str, ...] = tuple(f"hzrkl_{z}_rule" for z in ZONES)
# v003: gemessene hzrkl_{z}_on/_last (+ Lags) raus (Live-Lücke), Regel rein; _pos bleibt.
HZRKL_INPUTS: tuple[str, ...] = tuple(
    list(HZRKL_POS_TARGETS)
    + [f"hzrkl_{z}_pos_last" for z in ZONES]
    + [f"{c}_lag{k}" for k in _LAGS for c in HZRKL_POS_TARGETS]
    + list(HZRKL_RULE_COLS)
    + [f"hzrkl_{z}_rule_last" for z in ZONES]
    + [f"{c}_lag{k}" for k in _LAGS for c in HZRKL_RULE_COLS]
)
EXTRA_RAW_INPUTS: tuple[str, ...] = (
    "pump_hz_last", "oel_burner_last", "pump_solar_last",
    "sunrise_min", "sunset_min", "daylight_min", "wx_is_forecast", "gap_partial",
)
# Laufzeit-Fenster (m1.3, SCHEMA §2.14): rollierende Summe der An-Quotienten inkl.
# aktueller Stunde bzw. Stunden seit letzter Aktivität (0..48, 49 = nichts in 49 h).
RUNTIME_SIGNALS: tuple[str, ...] = (
    "oel_burner_on", "pump_wood_est_on", "wood_fire_on", "pump_ww_on", "pump_ww_oil_on",
    "pump_circ_on", "pump_hz_on", "pump_solar_on", "valve_hzrl_on",
)
RUNTIME_WINDOWS: tuple[int, ...] = (3, 6, 12, 24)
RUNTIME_INPUTS: tuple[str, ...] = tuple(
    [f"{s}_sum{w}h" for w in RUNTIME_WINDOWS for s in RUNTIME_SIGNALS]
    + [f"{s}_since_h" for s in RUNTIME_SIGNALS]
)
# Anlagen-Proxys (m1.3): O1-Holzpumpe geschätzt, Holzbrand (Abgas > 100 °C), O9-Marge.
PLANT_INPUTS: tuple[str, ...] = ("pump_wood_est_on", "wood_fire_on", "o9_switch_margin")
# An-Quotienten der aktuellen Stunde, die M2 als „Ergebnis der Stunde“ ausschloss
# (m2.config.DIGITAL_ACTION_EXCLUDED). Entscheidung v003 (docs/l1-model-v003.md §3.2):
# **aufnehmen** — kein Leakage. Input-Stunde t ist bei der Vorhersage abgeschlossen
# (Targets ab t+1), die zugehörigen ``*_last`` waren schon in v002 Input und
# ``{sig}_sum3h``/``_since_h`` enthalten die Stunde t ohnehin. Messung 2026-11-01 (mit vs.
# ohne, Valid/Test): Puffer +4 h ±1 %, oel_burner_on +1 h −1,3/−3,0 %, hzrkl_eg_on +1 h
# −5,1/−6,1 %, Gain-Anteil an buff_mid +4 h zusammen 0,5 %, Responsivität EG −0,615 =
# −0,615 → kleiner, konsistenter Nutzen auf Valid **und** Test, kein Leakage-Sprung.
CURRENT_ON_INPUTS: tuple[str, ...] = ("oel_burner_on", "pump_hz_on", "pump_solar_on")
RAW_INPUTS: tuple[str, ...] = (
    BASE_INPUTS + LAST_INPUTS + AGE_INPUTS + EXTRA_RAW_INPUTS + HZRKL_INPUTS
    + RUNTIME_INPUTS + PLANT_INPUTS + tuple(c for c in CURRENT_ON_INPUTS if c not in BASE_INPUTS)
)
# Neu in v003 gegenüber v002 (Rohspalten; Bericht der Feature-Importance):
V003_NEW_RAW_INPUTS: tuple[str, ...] = (
    tuple(c for c in HZRKL_INPUTS if "_rule" in c) + RUNTIME_INPUTS + PLANT_INPUTS
    + tuple(c for c in CURRENT_ON_INPUTS if c not in BASE_INPUTS)
)
# Für die Ziel-Zeilen (t+h) zusätzlich benötigte Rohspalten:
TARGET_AUX: tuple[str, ...] = ("gap_full",) + AGE_INPUTS

# Abgeleitete Features (m2b.features.derive): Namen hier nur dokumentiert.
DIFF_COLS: tuple[str, ...] = (
    "ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "buff_top", "buff_mid",
    "buff_bot", "hz_vl", "hz_rl", "ww", "ext",
)
WX_AGG_WINDOWS: tuple[int, ...] = (4, 6, 12, 24)

# --- Responsivität (P3) ---
RESP_N_CASES = 200
RESP_DELTAS_K: tuple[float, ...] = (1.0, 2.0, 3.4, 5.0)
RESP_TARGET_DELTA = 3.4
# DoD-Zone = EG (Q-110 maintainer 2026-11-01: EG hat die größte Heizlast/wärmsten Räume,
# OG kühlt am schnellsten aus, Keller am konstantesten/kaum Heizbedarf). Bericht für alle Zonen.
RESP_ZONE = "eg"
RESP_ZONES: tuple[str, ...] = ("eg", "dg", "kell")
RESP_HORIZONS: tuple[int, ...] = (1, 4, 6)
RESP_CORR_MAX = -0.6
RESP_FRAC_MIN = 0.8
RESP_HEATING_EXT_MAX = 12.0  # nur Heizsaison-Zustände (Außen-T Stundenmittel < 12 °C)


def resp_effects(zone: str) -> tuple[str, ...]:
    """Wirkungsgrößen je Zone in Priorität; die erste trainierte ist die DoD-Größe.

    Rücklaufminderer der Etage (direktes Stellglied, Q-110) vor gemeinsamer Pumpe d8.
    """
    return (f"hzrkl_{zone}_on", f"hzrkl_{zone}_pos", "pump_hz_on")


# --- Heizkreis-Regler (UVR1611 u1611FunkHeizkreis, docs/uvr-programm-analyse.md §2.2) ---
# RT-Eingang = MIN(perzipierte RAS), Norm 20 °C / Absenk 16 °C, Zeitprogramm 06:00–22:30.
RT_NORM = 20.0
RT_ABSENK = 16.0
RT_DAY_START_H = 6.0
RT_DAY_END_H = 22.5

# --- Regler-Targets: sehen nur, was der Regler sieht (P3) ---
# Die Heizkreis-Pumpe d8 wird vom Regler aus RT = MIN(perzipiert) geschaltet; die
# gemessene Ist-Temperatur (perzipiert − Offset) und die RAS-/Offset-Historie sind
# für die Schaltentscheidung nicht kausal. Bleiben sie sichtbar, verteilt das Modell
# den Raum-Einfluss auf Merkmale, die ein Offset-Schritt nicht verschiebt →
# Offset-Wirkung verwässert (Experiment 2026-10-31: Pearson −0,34 → −0,50).
# Gleiches gilt für die Rücklaufminderer je Etage (UVR610 schaltet nach der
# perzipierten Temperatur der eigenen Etage).
CONTROLLER_TARGETS: tuple[str, ...] = ("pump_hz_on",) + HZRKL_ON_TARGETS

# Kausal-Modus für Regler-Targets (Entscheidung 2026-11-01, docs/l1-model-v002.md §4.3):
# monotone Variante erzwingen + Early-Stopping auf L1 statt L2. L2-Stopp endet nach
# 25–33 Runden (+4/+6 h) → Modell ≈ Klimatologie, Offset-Wirkung − 0,13 (Pearson);
# L1-Stopp 174–800 Runden → Pearson −0,46, Vorzeichen 98 % korrekt, Kosten: RMSE
# +4 h 0,346 → 0,373 (B0 0,363). L2 braucht die Aktionswirkung, nicht die letzte
# Nachkommastelle des Pumpen-Anteils → True. ``False`` = genauigkeitsoptimal.
CONTROLLER_CAUSAL = True

_RUNTIME_RE = re.compile(r"^.+_(sum\d+h|since_h)$")


def excluded_features(target: str, feature_names: list[str]) -> list[str]:
    """Für ``target`` ausgeblendete Features (werden als NaN = „fehlt“ übergeben)."""
    if target not in CONTROLLER_TARGETS:
        return []
    out = []
    for n in feature_names:
        if n.startswith("ras_") and "_ist" in n:
            out.append(n)  # Ist-Temperatur inkl. Lags/last/trend/d1/ist_min
        elif "_perc_lag" in n or (n.startswith("offset_") and "_lag" in n):
            out.append(n)  # RAS-/Offset-Historie (Schritt passiert „jetzt“)
        elif n.startswith("hzrkl_") and "_lag" in n:
            out.append(n)  # Ventil-/Regel-Historie inkl. hzrkl_*_rule_lag* (alte perzipierte T)
        elif _RUNTIME_RE.match(n):
            out.append(n)  # Laufzeit-Fenster {sig}_sum{w}h / {sig}_since_h (Historie)
    return out


# --- Monotonie (P3): Feature → Vorzeichen je Target ---
# Offset/perzipierte Temperatur ↑ ⇒ Regelung „sieht wärmer“ ⇒ Pumpe ↓ / RAS-Ist-Δ ↓.
# rt_err = RT-Soll − RT ↑ (zu kalt) ⇒ Pumpe ↑.
def monotone_spec(target: str) -> dict[str, int]:
    """Monotonie-Vorgaben (Feature → −1/+1) für ein Target (leer = keine)."""
    if target == "pump_hz_on":
        spec = {"ras_perc_min": -1, "ras_perc_min_last": -1, "rt_err": 1, "rt_err_last": 1}
        for z in ZONES:
            spec[f"offset_{z}"] = -1
            spec[f"ras_{z}_perc"] = -1
            spec[f"ras_{z}_perc_last"] = -1
        return spec
    if target in HZRKL_ON_TARGETS:
        # Etagen-Minderer: perzipiert/Offset der eigenen Etage ↑ ⇒ Ventil zu.
        z = ZONE_OF_HZRKL[target]
        return {
            f"offset_{z}": -1, f"ras_{z}_perc": -1, f"ras_{z}_perc_last": -1,
            # Regel-Zustand der Etage (offen = 1) ⇒ Ventil-Anteil ↑ (v003).
            f"hzrkl_{z}_rule": 1, f"hzrkl_{z}_rule_last": 1,
        }
    if target in RAS_IST_TARGETS:
        z = ZONE_OF_RAS[target]
        return {f"offset_{z}": -1}
    if target in RAS_PERC_TARGETS:
        # perzipiert = Ist + Offset: Δperc enthält den (gehaltenen) Offset nicht;
        # mehr Offset ⇒ weniger Heizen ⇒ Ist-Anteil ↓ (gleiche Richtung wie Ist).
        z = ZONE_OF_RAS[target]
        return {f"offset_{z}": -1}
    return {}


MONOTONE_TARGETS: tuple[str, ...] = ("pump_hz_on",) + RAS_TARGETS + HZRKL_ON_TARGETS

# --- LightGBM ---
LGBM_NUM_BOOST = 3000
LGBM_EARLY_STOP = 100
LGBM_THREADS_DEFAULT = 16


def lgbm_params(kind: str, seed: int = SEED, threads: int = LGBM_THREADS_DEFAULT) -> dict:
    """Deterministische LightGBM-Parameter (``kind`` = 'analog' | 'digital').

    Early-Stopping-Metrik: Analog L1 (= DoD-MAE), Digital L2 (= Brier/RMSE des
    An-Quotienten; L1 würde 0/1-Mediane statt Erwartungswerte belohnen).
    """
    return {
        "objective": "regression_l1" if kind == "analog" else "cross_entropy",
        "metric": "l1" if kind == "analog" else "l2",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 50,
        "feature_fraction": 0.5,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        "max_bin": 255,
        "seed": int(seed),
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": int(threads),
        "verbosity": -1,
    }


# --- Keras v2 (Referenz, P4) ---
KERAS_HIDDEN: tuple[int, ...] = (256, 128)
KERAS_DROPOUT = 0.1
KERAS_LR = 1e-3
KERAS_BATCH = 512
KERAS_EPOCHS = 150
KERAS_PATIENCE = 15
KERAS_HUBER_DELTA = 1.0  # im z-Raum der Targets

# --- DoD (v002 = v003) ---
DOD_RAS_MAE_MAX = 1.0
DOD_BUFF_MAE_MAX = 1.5
PHYS_ABS_MAX = 150.0  # |Prädikat| > 150 °C ⇒ Explosion (DoD-P0/P4)

TARGET_KIND: dict[str, str] = {t: "analog" for t in ANALOG_TARGETS} | {
    t: "digital" for t in DIGITAL_TARGETS
}
