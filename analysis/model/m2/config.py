"""M2 config: Spalten-Definitionen, Loss-Gewichte, Seeds, Pfade.

Spaltennamen sind kanonisch aus M1-SCHEMA (``analysis/model/m1/SCHEMA.md``) und
``docs/plant-model.json``. Kollektor = ``solar_coll`` (dev2 a16), Ölbrenner =
``oel_burner_on`` (dev0 d5), Pumpe Heizkreis Keller = ``pump_hz_on`` (dev0 d8),
Solarpumpe = ``pump_solar_on`` (dev2 d1). Zeitzone: Europe/Berlin (naive
Kalenderstunden aus M1-Feature-Store).
"""
from __future__ import annotations

from pathlib import Path

# --- Pfade (relative zum Repo-Root; __file__-basiert, wie M1) ---
# parents: [0]=m2, [1]=model, [2]=analysis, [3]=cmi (Repo-Root)
_REPO_ROOT = Path(__file__).resolve().parents[3]
FEATURES_DIR = _REPO_ROOT / "data" / "features"
M2_DIR = _REPO_ROOT / "analysis" / "model" / "m2"
MODELS_DIR = _REPO_ROOT / "models"
ARTIFACT_DIR = MODELS_DIR / "cmi-brain" / "v001"
ARTIFACT_NAME = "cmi-brain-v001"
RUNS_DIR = M2_DIR / "runs"
CHECKPOINT_DIR = RUNS_DIR / "checkpoint"
ARTIFACT_READY_FLAG = RUNS_DIR / "artifact_ready.json"
REPORT_PATH = M2_DIR / "REPORT.md"


def rel_to_repo(p: Path) -> Path:
    """Pfad relativ zum Repo-Root (nur für Log-/Report-Anzeige).

    Liefert den **absoluten** Pfad zurück, wenn ``p`` nicht unter dem
    Repo-Root liegt (z. B. tmp_path in Tests) — verhindert ``ValueError``
    aus ``Path.relative_to``.
    """
    try:
        return p.relative_to(_REPO_ROOT)
    except ValueError:
        return p

# --- Datenversion (M1) & Seed ---
DATA_VERSION = "m1.0"
SEED = 20261031

# --- Walk-forward-Splits (verbindlich) ---
VALID_START = "2025-01-01"
VALID_END = "2026-01-01"
TEST_START = "2026-01-01"
TRAIN_END = "2025-01-01"  # train = ts < TRAIN_END

# --- Target-Kanäle (1-h-ahead; Reihenfolge = Keras-Head-Reihenfolge) ---
# Analog-Targets (°C bzw. h/h bzw. K):
ANALOG_TARGETS: tuple[str, ...] = (
    "ras_kell_ist",
    "ras_eg_ist",
    "ras_dg_ist",
    "ras_kell_perc",
    "ras_eg_perc",
    "ras_dg_perc",
    "offset_kell",
    "offset_eg",
    "offset_dg",
    "hz_vl",
    "hz_rl",
    "buff_top",
    "buff_mid",
    "buff_bot",
    "ww",
    "solar_coll",  # Kollektor (dev2 a16; NULL vor 2022-03-24 → Median-Imputation)
    "wood_vl",
    "flue",
    "oil_vl",
    "heat_min_kell",
    "heat_min_eg",
    "heat_min_dg",
)
# Digital-Targets (An-Quotient 0..1): d5, d8 + übrige dev0 d1–d9 + dev2 d1
DIGITAL_TARGETS: tuple[str, ...] = (
    "oel_burner_on",  # dev0 d5 Ölbrenner (An-Quotient Ziel-Bucket)
    "pump_hz_on",  # dev0 d8 Pumpe Heizkreis Keller
    "pump_ww_on",  # dev0 d2
    "mix_open_on",  # dev0 d3
    "mix_close_on",  # dev0 d4
    "pump_ww_oil_on",  # dev0 d6
    "pump_circ_on",  # dev0 d7
    "valve_hzrl_on",  # dev0 d9
    "pump_solar_on",  # dev2 d1
)
TARGETS: tuple[str, ...] = ANALOG_TARGETS + DIGITAL_TARGETS
N_TARGETS = len(TARGETS)  # 31
N_DIGITAL = len(DIGITAL_TARGETS)  # 9

# --- Loss-Gewichte (Initial-Values, dokumentiert; Tuning nur via Valid) ---
LOSS_WEIGHTS: dict[str, float] = {
    "ras_kell_ist": 1.0,
    "ras_eg_ist": 1.0,
    "ras_dg_ist": 1.0,
    "ras_kell_perc": 1.0,
    "ras_eg_perc": 1.0,
    "ras_dg_perc": 1.0,
    "offset_kell": 0.8,
    "offset_eg": 0.8,
    "offset_dg": 0.8,
    "hz_vl": 0.7,
    "hz_rl": 0.7,
    "buff_top": 0.7,
    "buff_mid": 0.7,
    "buff_bot": 0.7,
    "heat_min_kell": 0.5,
    "heat_min_eg": 0.5,
    "heat_min_dg": 0.5,
    "ww": 0.4,
    "oil_vl": 0.4,
    "wood_vl": 0.4,
    "flue": 0.4,
    "solar_coll": 0.4,
    "oel_burner_on": 0.4,
    "pump_hz_on": 0.4,
    "pump_ww_on": 0.4,
    "mix_open_on": 0.4,
    "mix_close_on": 0.4,
    "pump_ww_oil_on": 0.4,
    "pump_circ_on": 0.4,
    "valve_hzrl_on": 0.4,
    "pump_solar_on": 0.4,
}

# --- Inputs ---
# RAS-Offset je Zone = Stellgröße / **Aktion** (historisch = von der
# CMI-/RAS-Logik gesetzt). Erscheint **genau einmal** (in ACTION_INPUTS) —
# nicht zusätzlich in STATE_INPUTS, da der Offset die Soll-Temperatur und
# nicht ein gemessener State-Wert ist (ein doppeltes Vorkommen würde im
# Rollout-Index-Map nur die Aktion-Kopie aktualisieren lassen → veraltete
# State-Kopie; s. m2.rollout).
OFFSET_INPUTS: tuple[str, ...] = ("offset_kell", "offset_eg", "offset_dg")
# State-Inputs (aktuelle Stunde; alle analogen Targets **außer** den
# RAS-Offsets (-> ACTION_INPUTS) + ext).
STATE_INPUTS: tuple[str, ...] = tuple(
    t for t in ANALOG_TARGETS if t not in OFFSET_INPUTS
) + ("ext",)
# Lags: RAS perc/ist/offset ×3 (9) + hz_vl, hz_rl, buff ×3, ww, ext, d5, d8,
# heat_min ×3 (15) → 24 Kanäle × 5 Lags (1/2/6/12/24 h) = 120 Spalten.
LAG_COLS: tuple[str, ...] = (
    "ras_kell_perc",
    "ras_kell_ist",
    "offset_kell",
    "ras_eg_perc",
    "ras_eg_ist",
    "offset_eg",
    "ras_dg_perc",
    "ras_dg_ist",
    "offset_dg",
    "hz_vl",
    "hz_rl",
    "buff_top",
    "buff_mid",
    "buff_bot",
    "ww",
    "ext",
    "oel_burner_on",
    "pump_hz_on",
    "heat_min_kell",
    "heat_min_eg",
    "heat_min_dg",
)
LAG_HOURS: tuple[int, ...] = (1, 2, 6, 12, 24)
LAG_INPUTS: tuple[str, ...] = tuple(
    f"{c}_lag{h}" for c in LAG_COLS for h in LAG_HOURS
)
# Aktion A_t: Digital-States (An-Quotient) + RAS-Offset je Zone.
# Historisch = was die CMI-/RAS-Logik gesetzt hat (Aktionen sind Regel-Folgen).
DIGITAL_ACTIONS: tuple[str, ...] = DIGITAL_TARGETS  # d5, d8 + übrige dev0 d2–d9 + dev2 d1
DIGITAL_ACTION_EXCLUDED: tuple[str, ...] = (
    "oel_burner_on",
    "pump_hz_on",
    "pump_solar_on",
)
DIGITAL_ACTION_INPUTS: tuple[str, ...] = tuple(
    c for c in DIGITAL_TARGETS if c not in DIGITAL_ACTION_EXCLUDED
)  # 6 Kanäle (Kausalität: d5/d8/d_solar sind Ergebnisse der aktuellen Stunde)
ACTION_INPUTS: tuple[str, ...] = DIGITAL_ACTION_INPUTS + OFFSET_INPUTS
# Wetter: 24 h-Fenster (t → t+23), bias-korrigierte T bevorzugt (wx_t{k}_t_bias).
WX_HOURS: tuple[int, ...] = tuple(range(24))
WX_VARS: tuple[str, ...] = (
    "t_bias",  # bias-korrigierte 2m-Temperatur (M1: Roh-Grid-T + saisonales Bias-Profil)
    "dew",
    "rh",
    "cc",
    "prec",
    "rain",
    "ws",
    "wg",
    "wd",
    "swr",
    "wmo",
)
WX_INPUTS: tuple[str, ...] = tuple(
    f"wx_t{h}_{v}" for h in WX_HOURS for v in WX_VARS
)  # 24 × 11 = 264
# Kalender:
CAL_INPUTS: tuple[str, ...] = ("hour", "day_of_week", "is_weekend", "is_holiday", "is_dst")
# Flags (konstant/informativ, dokumentiert):
FLAG_INPUTS: tuple[str, ...] = ("mode_known", "offset_known")

FEATURE_INPUTS: tuple[str, ...] = (
    STATE_INPUTS + LAG_INPUTS + ACTION_INPUTS + WX_INPUTS + CAL_INPUTS + FLAG_INPUTS
)
N_FEATURES = len(FEATURE_INPUTS)

# DoD-Kanäle:
RAS_IST_TARGETS: tuple[str, ...] = ("ras_kell_ist", "ras_eg_ist", "ras_dg_ist")
BUFF_TARGETS: tuple[str, ...] = ("buff_top", "buff_mid", "buff_bot")

# --- Modell-Hyperparameter ---
HIDDEN: tuple[int, ...] = (256, 128, 64)
DROPOUT = 0.1
HEAD_HIDDEN = 32
MAX_PARAMS = 500_000
BATCH_SIZE = 256
# LR: 3e-4 (nicht 1e-3): gewichtetes Multi-Head-MAE + Adam → 1e-3 oszilliert
# (val-Loss-Periode ~5 Epochs, Empirie v001-Run); 3e-4 + ReduceLROnPlateau
# (build.train_keras) konvergieren stabil.
LEARNING_RATE = 3e-4
EPOCHS_DEFAULT = 120
PATIENCE = 30
LGBM_NUM_BOOST = 3000  # M2b-P0: 500 war Deckel (best_iter 491/497)
LGBM_EARLY_STOPPING_ROUNDS = 100
# Rollout:
ROLLOUT_H = 4  # DoD-Horizont (4-h-Metrik)
# Offset-Responsivität:
RESP_N_CASES = 200
RESP_DELTAS_K: tuple[float, ...] = (1.0, 2.0, 3.4, 5.0)
RESP_HORIZON_H = 6
RESP_TARGET_DELTA = 3.4  # 18.10-Referenz-Step


def wx_input_cols() -> tuple[str, ...]:
    """Explizite Wetter-Spaltennamen (Alias für Tests/Re-Check)."""
    return WX_INPUTS


def loss_weights_array() -> tuple[float, ...]:
    """Loss-Gewichte in TARGETS-Reihenfolge (NumPy-fertig)."""
    return tuple(float(LOSS_WEIGHTS[t]) for t in TARGETS)
