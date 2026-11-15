"""Fixtures: Mini-Feature-Store (Hive, m1.3-Namen) + FakePredictor + WxLive.

Kein echtes Training, kein CMI-Zugriff: die Tests nutzen einen kleinen
synthetischen Store (einige Stunden, alle nötigen Spalten) und einen
:func:`FakePredictor`, der die L1-Semantik (Delta → perzipiert, über
Horizont gehalten) nachbildet.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.l2 import weather
from analysis.model.l2.recommend import ROLLOUT_TARGETS, load_state

# --- Mini-Store ---

T0 = "2026-11-01 11:00"  # erste Zeile (aware Berlin → naive Wallclock im Store)
N_ROWS = 5  # 11:00 … 15:00


def make_mini_store(root: Path, kell_ist: float = 16.5) -> Path:
    """Kleiner Hive-Store (5 Stunden, m1.3-Namen, ``data_version`` = m1.3).

    ``kell_ist``: Keller-Ist-T; Default 16.5 → perzipiert 20.0 (im RT-Soll-Band,
    „flacher“ Zustand). 21.8 → perzipiert 25.3 (5 K über Soll, Szenario für
    Keller-Offset-Senkung).
    """
    ts = np.array([f"2026-11-01 {11 + i:02d}:00" for i in range(N_ROWS)], dtype="datetime64[h]")
    n = len(ts)
    cols: dict[str, np.ndarray | None] = {}  # None = NULL-Spalte (v0.4)
    # RAS-Offsets (plausibel, 0,1-K-Auflösung wie im echten Store).
    cols["offset_kell"] = np.array([3.5] * n)
    cols["offset_eg"] = np.array([-1.8] * n)
    cols["offset_dg"] = np.array([1.4] * n)
    cols["ras_kell_ist"] = np.array([kell_ist] * n)
    cols["ras_eg_ist"] = np.array([21.9] * n)
    cols["ras_dg_ist"] = np.array([19.1] * n)
    cols["ras_kell_perc"] = cols["ras_kell_ist"] + cols["offset_kell"]
    cols["ras_eg_perc"] = cols["ras_eg_ist"] + cols["offset_eg"]
    cols["ras_dg_perc"] = cols["ras_dg_ist"] + cols["offset_dg"]
    # v003-Spalten (apply_offset_step verschiebt perc + perc_last + Offset):
    cols["ras_kell_perc_last"] = cols["ras_kell_ist"] + cols["offset_kell"]
    cols["ras_eg_perc_last"] = cols["ras_eg_ist"] + cols["offset_eg"]
    cols["ras_dg_perc_last"] = cols["ras_dg_ist"] + cols["offset_dg"]
    cols["ras_age_kell"] = np.zeros(n)
    cols["ras_age_eg"] = np.zeros(n)
    cols["ras_age_dg"] = np.zeros(n)
    cols["buff_top"] = np.array([62.0, 63.0, 63.5, 64.0, 64.5])
    cols["buff_mid"] = cols["buff_top"] - 2.0
    cols["buff_bot"] = cols["buff_top"] - 4.0
    cols["hz_vl"] = np.array([32.0, 33.0, 33.5, 34.0, 34.5])
    cols["hz_rl"] = cols["hz_vl"] + 2.0
    cols["ext"] = np.array([9.0, 8.5, 8.0, 7.5, 7.0])
    cols["ww"] = np.array([47.0] * n)
    cols["solar_coll"] = np.array([28.0] * n)
    cols["flue"] = np.array([95.0] * n)
    cols["oel_burner_on"] = np.array([0.0] * n)
    cols["oel_burner_last"] = np.array([0.0] * n)
    cols["oel_burner_on_sum12h"] = np.zeros(n)
    cols["oel_burner_on_sum24h"] = np.zeros(n)
    cols["oel_burner_on_since_h"] = np.array([49.0] * n)
    cols["oel_burner_on_sum3h"] = None  # v0.4: NULL → Guard stumm (Fail-Open)
    cols["pump_hz_on"] = np.array([0.0] * n)
    cols["pump_hz_last"] = np.array([0.0] * n)
    cols["pump_hz_on_sum3h"] = None  # v0.4: NULL → Guard stumm (Fail-Open)
    cols["pump_hz_on_sum12h"] = np.zeros(n)
    cols["pump_hz_on_sum24h"] = np.zeros(n)
    cols["pump_hz_on_since_h"] = np.array([49.0] * n)
    cols["pump_circ_on"] = np.array([0.2] * n)
    cols["pump_circ_on_sum24h"] = np.array([5.0] * n)
    cols["gap_full"] = np.zeros(n)
    cols["gap_partial"] = np.zeros(n)
    # Effektor-Guard (Fix-2): Rücklaufminderer-Offen-Anteil je Zone
    # (3-h-Fenster: rule, lag1, lag2). Default 1,0 = offen/normal → alle
    # bestehenden Synthetic-Tests behalten die Normal-Regel-Behandlung
    # (Guard stumm; Fix-1-Tests dürfen nicht brechen).
    for z in ("kell", "eg", "dg"):
        cols[f"hzrkl_{z}_rule"] = np.ones(n)
        cols[f"hzrkl_{z}_rule_lag1"] = np.ones(n)
        cols[f"hzrkl_{z}_rule_lag2"] = np.ones(n)
    # v0.5 Ticket D (ΔT-Diagnose + Lag + Konditions-Flag): Minderer-offen-
    # 2-h-Fenster (on + lag1), Strang-RL-Fühler (hzrkl_{z}_pos + _last),
    # VL-Lag-1h (hz_vl_lag1). Default = "heizt offen" (on 1,0; RL ~30–31 °C),
    # sodass die Konditions-Flag standardmäßig aktiv ist. Deterministisch.
    for z in ("kell", "eg", "dg"):
        cols[f"hzrkl_{z}_on"] = np.ones(n)
        cols[f"hzrkl_{z}_on_lag1"] = np.ones(n)
    cols["hzrkl_kell_pos"] = np.array([30.0] * n)
    cols["hzrkl_kell_pos_last"] = np.array([29.5] * n)
    cols["hzrkl_eg_pos"] = np.array([31.0] * n)
    cols["hzrkl_eg_pos_last"] = np.array([30.5] * n)
    cols["hzrkl_dg_pos"] = np.array([31.0] * n)
    cols["hzrkl_dg_pos_last"] = np.array([30.5] * n)
    cols["hz_vl_lag1"] = np.array([31.5, 32.5, 33.0, 33.5, 34.0])
    cols["data_version"] = np.array(["m1.3"] * n, dtype=object)
    # Wetter-Spalten (Store-Fallback + Injektions-Zielfläche).
    for k in range(24):
        cols[f"wx_t{k}_t_bias"] = np.array([9.0 - 0.1 * k] * n)
        cols[f"wx_t{k}_swr"] = np.array([100.0] * n)
    p = root / "2026" / "11" / "2026-11.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    tbl: dict[str, Any] = {"ts": pa.array(ts.astype("datetime64[us]"))}
    for c, v in cols.items():
        if c == "data_version":
            tbl[c] = pa.array([str(x) for x in v])
        elif v is None:
            # NULL-Spalte (v0.4-Heizbetrieb-Guard): alle Zeilen NULL → Guard stumm.
            tbl[c] = pa.array([None] * n, type=pa.float64())
        else:
            tbl[c] = pa.array(v.astype(np.float64))
    pq.write_table(pa.table(tbl), p)
    return root


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return make_mini_store(tmp_path / "features")


@pytest.fixture()
def store_hot(tmp_path: Path) -> Path:
    """Mini-Store mit Keller perzipiert über Soll (Ist 21.8 → perzipiert 25.3)."""
    return make_mini_store(tmp_path / "features", kell_ist=21.8)


@pytest.fixture()
def st(store: Path):
    """State für die ts-Zeile (vollständig vorhanden, Lücke 0) + Store-Pfad."""
    s = load_state("2026-11-01 11:00", features_dir=store)
    s.store_dir = store
    return s


@pytest.fixture()
def st_hot(store_hot: Path):
    """State (Keller perzipiert 25.3, 5 K über Soll) + Store-Pfad."""
    s = load_state("2026-11-01 11:00", features_dir=store_hot)
    s.store_dir = store_hot
    return s


@pytest.fixture()
def store_full(tmp_path: Path) -> Path:
    """Mini-Store, der ALLE 4 Empfehlungstypen triggert:

    - Heizkurve/VL-Soll (Immer aktiv, Abweichung ≥ 2 K),
    - WW-Ladefenster (Puffer < 57 °C → Marge > 0),
    - Zirkulation (15 h/24 h + Puffer ≥ 50 °C),
    - RAS-Offset ×3 (Tier A, immer).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = make_mini_store(tmp_path / "features_full")
    p = root / "2026" / "11" / "2026-11.parquet"
    table = pq.read_table(p)
    n = N_ROWS
    table = table.set_column(
        table.schema.get_field_index("buff_top"),
        pa.field("buff_top", pa.float64()),
        pa.array([55.0] * n),
    )
    table = table.set_column(
        table.schema.get_field_index("pump_circ_on_sum24h"),
        pa.field("pump_circ_on_sum24h", pa.float64()),
        pa.array([15.0] * n),
    )
    pq.write_table(table, p)
    return root


@pytest.fixture()
def st_full(store_full: Path):
    """State (alle 4 Empfehlungstypen aktiv) + Store-Pfad."""
    s = load_state("2026-11-01 11:00", features_dir=store_full)
    s.store_dir = store_full
    return s


def patch_state(st: Any, **over: Any) -> Any:
    """State-Zeile überschreiben (Skalare, nicht 1-elementige Arrays)."""
    for k, v in over.items():
        st.rows[k] = v
    return st


# --- FakePredictor (L1-Semantik: Offset wirkt auf perzipiert, über h gehalten) ---

FAKE_RAW_INPUTS: tuple[str, ...] = (
    "offset_kell",
    "offset_eg",
    "offset_dg",
    "ext",
    "buff_top",
    "ras_kell_ist",
    "ras_eg_ist",
    "ras_dg_ist",
    "pump_hz_on",
    "oel_burner_on",
    "wx_t0_t_bias",
    "wx_t0_swr",
)


class FakePredictor:
    """Deterministischer L1-Ersatz: perzipiert = (Ist+Offset+Δ) über h gehalten.

    ``mode``:
    - ``"no_change"``: alle Zonen flach am RT-Soll (20,1 °C = Soll 11:00) →
      Komfort 0, Energie 0 → nur Änderungs-Strafe → Basisaktion gewinnt
      (No-change-by-default).
    - ``"change_kell"``: Keller perzipiert über Soll (Ist+Offset+Δ, mit Δ
      verschoben) → Keller-Δ senken spart Komfort → beste Aktion = Keller-
      Offset senken.
    """

    horizons: tuple[int, ...] = (1, 4, 6, 12)

    def __init__(self, mode: str = "no_change"):
        self.mode = mode
        self.raw_inputs: list[str] = list(FAKE_RAW_INPUTS)
        self.data_version = "m1.3"
        # Basis-Offsets des Mini-Stores (für die Δ-Ermittlung aus dem
        # apply_offset_step-verschobenen Offset).
        self.offset_base: dict[str, float] = {"kell": 3.5, "eg": -1.8, "dg": 1.4}
        # v0.3-FIX: RAS-Feedback-Gain — Offset-Änderung verschiebt das
        # Perzipiert-Ziel (gain 1,0: perz(h>0) reagiert voll gegen den
        # Offset-Step; 0,0 = Buchführung nur). „change_kell": Keller regelt.
        self.feedback: dict[str, float] = {"kell": 1.0, "eg": 0.0, "dg": 0.0}

    def _perc(self, raw: dict[str, np.ndarray], zone: str) -> float:
        return float(np.asarray(raw[f"ras_{zone}_perc"], dtype=np.float64)[0])

    def _delta(self, raw: dict[str, np.ndarray], zone: str) -> float:
        off = float(np.asarray(raw[f"offset_{zone}"], dtype=np.float64)[0])
        return off - self.offset_base[zone]

    def predict_raw(
        self,
        raw: dict[str, np.ndarray],
        ts: np.ndarray,
        targets: list[str] | None = None,
        horizons: list[int] | None = None,
    ) -> dict[tuple[str, int], np.ndarray]:
        out: dict[tuple[str, int], np.ndarray] = {}
        for t in targets or list(ROLLOUT_TARGETS):
            for h in horizons or list(self.horizons):
                out[(t, int(h))] = np.array([self._val(t, raw)], dtype=np.float64)
        return out

    def _val(self, t: str, raw: dict[str, np.ndarray]) -> float:
        if t.startswith("ras_") and "_perc" in t:
            zone = t.split("_")[1]
            if self.mode == "no_change":
                # Flacher Zustand: alle Zonen perzipiert am RT-Soll (11:00 =
                # 20.0) → Komfort 0, unabhängig von Offset/Δ.
                return 20.1
            # "change_kell": Rollout-Semantik (v0.3-FIX): apply_offset_step
            # verschiebt ``ras_{zone}_perc`` um Δ (h=0-Buchführung); dazu
            # RAS-Feedback (gain): das Perzipiert-Ziel reagiert auf den
            # Offset-Step (Offset-RAUF kühlt, Offset-AB heizt) und wird über
            # den Horizont gehalten.
            return self._perc(raw, zone) - self.feedback[zone] * self._delta(raw, zone)
        if t in ("oel_burner_on", "pump_hz_on"):
            return float(np.asarray(raw[t], dtype=np.float64)[0])
        if t == "hz_vl":
            return 25.0  # unter VL-Referenz → VL-Energie-Term = 0
        return 0.0


@pytest.fixture()
def fake_no_change() -> FakePredictor:
    return FakePredictor("no_change")


@pytest.fixture()
def fake_change() -> FakePredictor:
    return FakePredictor("change_kell")


# --- WxLive-Helfer ---


def make_wx(
    ts: datetime | str,
    t_values: list[float] | None = None,
    swr: float = 100.0,
    gen_ms: float | None = None,
    bias_k: float = 0.0,
) -> weather.WxLive:
    """Künstlicher LIVE-Forecast (24 h) für Tests (kein Netzwerk)."""
    t0 = weather.berlin_hour(ts)
    hours = weather.window_utc(t0)
    n = len(hours)
    t_values = t_values if t_values is not None else [8.0] * n
    assert len(t_values) == n
    hourly: dict[str, list[float | None]] = {
        "temperature_2m": list(t_values),
        "dew_point_2m": [v - 2.0 for v in t_values],
        "relative_humidity_2m": [70.0] * n,
        "cloudcover": [40.0] * n,
        "cloudcover_low": [30.0] * n,
        "cloudcover_mid": [30.0] * n,
        "cloudcover_high": [30.0] * n,
        "precipitation": [0.0] * n,
        "rain": [0.0] * n,
        "wind_speed_10m": [5.0] * n,
        "wind_gusts_10m": [8.0] * n,
        "wind_direction_10m": [200.0] * n,
        "shortwave_radiation": [swr] * n,
        "weather_code": [1.0] * n,
    }
    t_bias = [v + bias_k for v in t_values]
    now = datetime.now(UTC)
    run = now - timedelta(hours=0.5)
    return weather.WxLive(
        ts=t0,
        utc_hour=hours,
        hourly=hourly,
        t_bias=t_bias,
        wx_quelle="Open-Meteo ICON-D2 (live)",
        wx_run=run.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        wx_age_h=0.5,
        fetched_at=run.astimezone(UTC).isoformat(),
        extra={"generationtime_ms": gen_ms},
    )


@pytest.fixture()
def wx_ok() -> weather.WxLive:
    """Mildes Wetter (min 12 h > 0 °C, etwas Solar) → keine Frost-/Solar-Gates."""
    return make_wx("2026-11-01 11:00", t_values=[6.0] * 24, swr=300.0, gen_ms=12.0)


@pytest.fixture()
def wx_frost() -> weather.WxLive:
    """Frost-Nacht (min 12 h ≤ 0 °C)."""
    return make_wx("2026-11-01 11:00", t_values=[-1.5] * 24, swr=0.0, gen_ms=12.0)


@pytest.fixture()
def wx_old() -> weather.WxLive:
    """Alter Run (> 2 h) → Stale-Guard (Wetter-Run)."""
    wx = make_wx("2026-11-01 11:00", t_values=[6.0] * 24, gen_ms=12.0)
    wx.wx_age_h = 3.5
    wx.wx_run = "2026-11-01 07:30 UTC"
    return wx


@pytest.fixture()
def skill_map() -> dict[tuple[str, int], float]:
    """Künstliche Skill-Map (positive Skills → Konfidenz > Floor)."""
    return {
        ("ras_kell_perc", 12): 0.4,
        ("ras_eg_perc", 12): 0.5,
        ("ras_dg_perc", 12): 0.3,
        ("oel_burner_on", 12): 0.2,
        ("pump_hz_on", 12): 0.1,
    }


def make_api_response(ts: datetime, n: int = 48, gen_ms: float = 0.1) -> dict[str, Any]:
    """Ganztägige Open-Meteo-FORECAST-Antwort (14 Variablen, n Stunden, UTC)."""
    start = ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    times = [ (start + timedelta(hours=i)).isoformat() for i in range(n)]
    hourly: dict[str, list] = {"time": times}
    for var in weather.config.WX_HOURLY_VARS:
        hourly[var] = [10.0 + 0.1 * i for i in range(n)]
    return {
        "latitude": weather.config.LAT,
        "longitude": weather.config.LON,
        "generationtime_ms": gen_ms,
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "elevation": 242.0,
        "hourly": hourly,
    }
