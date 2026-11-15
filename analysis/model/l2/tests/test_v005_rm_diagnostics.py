"""L2 v0.5 Ticket D — ΔT-Diagnose + pro-Etage-Lag + Konditions-Flag.

Tests für das neue diagnostische Fundament (``analysis/model/l2/rm_diagnostics``
+ Integration in ``recommend``):

1. **Konditions-Flag** (3 DoD-Fälle: alle zu / 1 aktiv / 2+ aktiv) +
   OR-Semantik + NULL-Fail-Open + Inaktiv → Block stumm (``per_zone`` leer).
2. **Lag-Regression** (synthetische Verschiebung): auf einem künstlichen
   10-s-Roh-Parquet (VL = Sinus, RL = verschiebter Sinus) wird der korrekte
   Lag gefunden (``estimate_lags``), Default-Fallback bei n klein (Keller),
   ``load_lag_estimate``/``lag_for_zone`` + ``_to_grid``/
   ``_dt_raw_lag_corrected``-Arithmetik.
3. **ΔT-Korrekt**: pro-Call ΔT (Raw-Fenster, lag-korrigiert) + stündlicher
   Store-Fallback; ΔT-Beleg gegen die Referenz-Beleg-Zahl (EG offen ≈ 4,1 K,
   n=360; Keller zu ≈ 10,6 K, n=531; DG offen ≈ 6,3 K, n=139) — die Methode
   (Hourly-Mean-ΔT, Minderer-offen-Klasse) reproduziert die Referenz-Zahlen
   (Beleg 2026-11-07, s. Modul-Doku).
4. **Integration**: ``recommend()`` trägt ``rm_diagnostics`` im JSON (aktiv +
   inaktiv), ``model_version=…+l2-v005``.

Synthetic-Store + synthetische Rohdaten; kein Live-Netz, kein CMI-Zugriff,
deterministisch (Seed fix).
"""
from __future__ import annotations

import json
import math
import shutil
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.l2 import config
from analysis.model.l2 import rm_diagnostics as rm
from analysis.model.l2.recommend import (
    load_lag_estimate,
    recommend,
)
from analysis.model.l2.tests.conftest import N_ROWS, make_mini_store
from analysis.model.l2.tests.test_v003_ist_kalibrierung import TS, FakePredictorV3

# --- Hilfskonstrukte --------------------------------------------------------


def _row(**over: Any) -> dict[str, Any]:
    """Store-Zeile mit v0.5-Default (alle Minderer offen, RAS am Intent,
    Pumpe-3-h-Summe 0) + Overrides (None = NULL)."""
    rows: dict[str, Any] = {
        "pump_hz_on_sum3h": 0.0,
        "hzrkl_kell_on": 1.0,
        "hzrkl_kell_on_lag1": 1.0,
        "hzrkl_eg_on": 1.0,
        "hzrkl_eg_on_lag1": 1.0,
        "hzrkl_dg_on": 1.0,
        "hzrkl_dg_on_lag1": 1.0,
        "ras_kell_perc": 20.0,
        "offset_kell": 0.0,
        "ras_eg_perc": 20.0,
        "offset_eg": 0.0,
        "ras_dg_perc": 20.0,
        "offset_dg": 0.0,
    }
    rows.update(over)
    return rows


def make_raw_parquet(root: Any, vl: np.ndarray, rl: dict[str, np.ndarray],
                     rm: dict[str, np.ndarray], pump: np.ndarray,
                     ts: np.ndarray,
                     soll: dict[str, np.ndarray] | None = None,
                     store: str = "winsol_pq") -> Any:
    """Synthetischer 10-s-Roh-Store (Hive, 71-Spalten-Schema-Teilmenge):
    ``<store>/2026/11/2026-11.parquet`` (Default ``winsol_pq`` — das echte
    ``winsol_pq`` hat KEINE a17–19-Sollspannungen, s.
    ``test_misch_store_soll_null_cast``).

    ``soll`` (optional): Minderer-Sollspannungen a17–19 je Zone (cmi_log_pq
    P181; Default = Spalten fehlen im Store → ``_read_raw_since`` castet
    NULL → ``minderer_soll_v`` = Default 10,0 V).
    """
    from pathlib import Path

    p = Path(root) / store / "2026" / "11" / "2026-11.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ts": pa.array([str(t) for t in ts], type=pa.string()),
        "dev0_analog_06": pa.array(vl, type=pa.float64()),
        "dev0_analog_07": pa.array(rl["central"], type=pa.float64()),
        "dev2_analog_13": pa.array(rl["kell"], type=pa.float64()),
        "dev2_analog_14": pa.array(rl["dg"], type=pa.float64()),
        "dev2_analog_15": pa.array(rl["eg"], type=pa.float64()),
        "dev2_analog_10": pa.array(rm["kell"], type=pa.float64()),
        "dev2_analog_11": pa.array(rm["eg"], type=pa.float64()),
        "dev2_analog_12": pa.array(rm["dg"], type=pa.float64()),
        "dev0_digital_08": pa.array(pump, type=pa.float64()),
    }
    if soll is not None:
        data["dev2_analog_17"] = pa.array(soll["kell"], type=pa.float64())
        data["dev2_analog_18"] = pa.array(soll["eg"], type=pa.float64())
        data["dev2_analog_19"] = pa.array(soll["dg"], type=pa.float64())
    pq.write_table(pa.table(data), p)
    return root


def _sine_signal(n: int, amp: float = 3.0, base: float = 35.0, period_s: int = 7200,
                 phase: float = 0.0) -> np.ndarray:
    """Sinus (Periode 7200 s » Lag-Such-Range 3600 s → eindeutiger
    Kreuzkorrelations-Peak, kein Aliasing mehrerer Lags)."""
    t = np.arange(n) * 10
    return base + amp * np.sin(2 * math.pi * t / period_s + phase)


def _sine_ts(n: int) -> np.ndarray:
    return np.datetime64("2026-11-05 00:00:00") + np.arange(n) * np.timedelta64(10, "s")


def _active_zone_rows(pump_on: float, rm_kell: float, rm_eg: float, rm_dg: float,
                      perz: dict[str, float] | None = None,
                      off: dict[str, float] | None = None) -> dict[str, Any]:
    rows = _row()
    rows["pump_hz_on_sum3h"] = pump_on
    rows["hzrkl_kell_on"] = rm_kell
    rows["hzrkl_kell_on_lag1"] = rm_kell
    rows["hzrkl_eg_on"] = rm_eg
    rows["hzrkl_eg_on_lag1"] = rm_eg
    rows["hzrkl_dg_on"] = rm_dg
    rows["hzrkl_dg_on_lag1"] = rm_dg
    for z, v in (perz or {}).items():
        rows[f"ras_{z}_perc"] = v
    for z, v in (off or {}).items():
        rows[f"offset_{z}"] = v
    return rows


# --- 1) Konditions-Flag (3 DoD-Fälle + OR + Fail-Open + stumm) ---------------


def test_condition_fall1_alle_zu_inaktiv() -> None:
    """DoD-Fall 1 (alle Minderer zu, RAS am Intent, Pumpe aus) → inaktiv."""
    cond = rm.rm_condition(_active_zone_rows(0.0, 0.0, 0.0, 0.0))
    assert cond.active is False
    assert cond.rm_open_2h == {"kell": 0.0, "eg": 0.0, "dg": 0.0}
    assert cond.pump_3h_avg == 0.0
    assert cond.active_zones == []


def test_condition_fall2_ein_kreis_aktiv() -> None:
    """DoD-Fall 2 (nur EG offen, Pumpe aus) → aktiv über Minderer-Ø."""
    cond = rm.rm_condition(_active_zone_rows(0.0, 0.0, 1.0, 0.0))
    assert cond.active is True
    assert cond.active_zones == ["eg"]
    assert any("Minderer eg" in r for r in cond.reasons)


def test_condition_fall3_zwei_kreise_aktiv() -> None:
    """DoD-Fall 3 (EG + DG offen) → aktiv, 2 Zonen."""
    cond = rm.rm_condition(_active_zone_rows(0.0, 0.0, 1.0, 1.0))
    assert cond.active is True
    assert cond.active_zones == ["eg", "dg"]


def test_condition_pumpe_3h_eps() -> None:
    """Pumpen-3-h-Ø ≥ EPS (Summe 1,5 → Ø 0,5) → aktiv, alle Zonen."""
    cond = rm.rm_condition(_active_zone_rows(1.5, 0.0, 0.0, 0.0))
    assert cond.active is True
    assert cond.pump_3h_avg == 0.5
    assert cond.active_zones == ["kell", "eg", "dg"]


def test_condition_ras_gap_kalt_aktiv() -> None:
    """RAS perzipiert < Intent − 0,5 K (≥ 1 Zone) → aktiv.

    perz 19,0, Intent = 20 − 0 = 20 → Gap −1,0 K < −0,5 K."""
    cond = rm.rm_condition(_active_zone_rows(0.0, 0.0, 0.0, 0.0,
                                             perz={"kell": 19.0}))
    assert cond.active is True
    assert cond.active_zones == ["kell"]
    assert any("RAS kell" in r for r in cond.reasons)


def test_condition_ras_gap_am_intent_nicht_aktiv() -> None:
    """perz = Intent exakt (Gap 0) → nicht aktiv (Rand ≥ 0,5 K)."""
    cond = rm.rm_condition(_active_zone_rows(0.0, 0.0, 0.0, 0.0,
                                             perz={"eg": 20.0}))
    assert cond.active is False


def test_condition_offset_senkt_intent() -> None:
    """Offset +1 K senkt Intent (20−1=19): perz 18,8 → Gap −0,2 K (inaktiv);
    ohne Offset (Intent 20) → Gap −1,2 K (aktiv). Der Offset bestimmt also
    die Aktivierung über das Intent."""
    cond_off = rm.rm_condition(_active_zone_rows(0.0, 0.0, 0.0, 0.0,
                                                 perz={"dg": 18.8}, off={"dg": 1.0}))
    assert cond_off.active is False
    assert cond_off.ras_gap["dg"] == pytest.approx(-0.2, abs=1e-6)
    cond_no_off = rm.rm_condition(_active_zone_rows(0.0, 0.0, 0.0, 0.0,
                                                   perz={"dg": 18.8}, off={"dg": 0.0}))
    assert cond_no_off.active is True
    assert cond_no_off.ras_gap["dg"] == pytest.approx(-1.2, abs=1e-6)


def test_condition_null_fail_open() -> None:
    """NULL-Spalten → keine Aktivierung, keine Crashes (Fail-Open)."""
    cond = rm.rm_condition({"pump_hz_on_sum3h": None, "hzrkl_kell_on": None,
                            "hzrkl_kell_on_lag1": None, "hzrkl_eg_on": None,
                            "hzrkl_eg_on_lag1": None, "hzrkl_dg_on": None,
                            "hzrkl_dg_on_lag1": None, "ras_kell_perc": None,
                            "ras_eg_perc": None, "ras_dg_perc": None})
    assert cond.active is False
    assert cond.pump_3h_avg == 0.0
    assert cond.rm_open_2h["kell"] == 0.0


def test_condition_partial_null_zahlt_nicht() -> None:
    """Ein 2-h-Wert NULL → Ø über verbleibende Stunde (keine Halbe-Halbierung):
    EG on = (0,0,…) + lag1 NULL → Ø = 0 (nicht 0,5). Alle Kreise zu → inaktiv."""
    rows = _row()
    rows["hzrkl_kell_on"] = 0.0
    rows["hzrkl_kell_on_lag1"] = 0.0
    rows["hzrkl_dg_on"] = 0.0
    rows["hzrkl_dg_on_lag1"] = 0.0
    rows["hzrkl_eg_on"] = 0.0
    rows["hzrkl_eg_on_lag1"] = None
    cond = rm.rm_condition(rows)
    assert cond.rm_open_2h["eg"] == 0.0
    assert cond.active is False


# --- 2) Lag-Regression (synthetische Verschiebung) ---------------------------


def test_lag_regression_eg_geschoben(tmp_path: Any) -> None:
    """RL_EG um 30 s verschoben (VL = Sinus, RL = Sinus mit Phase +4,5°) →
    Kreuzkorrelation findet Lag ≈ 30 s (±20 s, Perioden-Aliasing-frei)."""
    n = 4000
    ts = _sine_ts(n)
    vl = _sine_signal(n)
    # RL_EG = VL 30 s in der Vergangenheit → RL(t) = VL(t − 30 s)
    # (Versatz: RL folgt VL mit +30 s Lag).
    rl_eg = vl.copy()
    rl_eg[3:] = vl[:-3]
    rl_dg = vl.copy()
    rl_kell = vl.copy()
    rl_central = vl.copy()
    # Minderer: EG offen, DG/Kell zu (n klein → Keller-Default-Fallback)
    rm_eg = np.full(n, 10.0)
    rm_kell = np.full(n, 0.0)
    rm_dg = np.full(n, 0.0)
    pump = np.ones(n)
    make_raw_parquet(tmp_path / "raw", vl, {"kell": rl_kell, "dg": rl_dg,
                                            "eg": rl_eg, "central": rl_central},
                     {"kell": rm_kell, "eg": rm_eg, "dg": rm_dg}, pump, ts)
    art = rm.estimate_lags(data_dir=tmp_path / "raw", window_days=30,
                           now="2026-11-06 00:00:00",
                           out_path=tmp_path / "lag.json")
    eg = art["zones"]["eg"]
    assert eg["source"] == "estimate"
    assert 10 <= eg["lag_s"] <= 50  # ≈ 30 s (Grid-Schritt 10 s)
    assert eg["confidence"] > 0.5
    assert eg["corr"] > 0.8
    # Keller: Minderer zu → n aktiv klein → Default-Lag-Fallback.
    kell = art["zones"]["kell"]
    assert kell["source"] == "default"
    assert kell["lag_s"] == config.RM_DEFAULT_LAG_S
    assert kell["confidence"] == 0.0
    # Artefakt persistiert + reloadbar.
    assert (tmp_path / "lag.json").exists()
    loaded = rm.load_lag_estimate(tmp_path / "lag.json")
    assert loaded["zones"]["eg"]["lag_s"] == eg["lag_s"]
    lag_s, conf, src = rm.lag_for_zone(loaded, "eg")
    assert (lag_s, src) == (eg["lag_s"], "estimate") and conf == eg["confidence"]
    lag_s, conf, src = rm.lag_for_zone(loaded, "kell")
    assert (lag_s, src) == (config.RM_DEFAULT_LAG_S, "default")


def test_lag_regression_dg_groesser_lag(tmp_path: Any) -> None:
    """DG-Lag = 50 s (EG = 30 s) → Lags sind pro Etage unterschiedlich."""
    n = 4000
    ts = _sine_ts(n)
    vl = _sine_signal(n)
    rl_eg = np.roll(vl, 3)   # 30 s
    rl_eg[:3] = vl[3]
    rl_dg = np.roll(vl, 5)   # 50 s
    rl_dg[:5] = vl[5]
    rl_kell = vl.copy()
    rm_v = {"kell": np.full(n, 10.0), "eg": np.full(n, 10.0), "dg": np.full(n, 10.0)}
    pump = np.ones(n)
    make_raw_parquet(tmp_path / "raw", vl, {"kell": rl_kell, "dg": rl_dg,
                                            "eg": rl_eg, "central": vl},
                     rm_v, pump, ts)
    art = rm.estimate_lags(data_dir=tmp_path / "raw", window_days=30,
                           now="2026-11-06 00:00:00",
                           out_path=tmp_path / "lag.json")
    assert art["zones"]["dg"]["lag_s"] > art["zones"]["eg"]["lag_s"]
    assert art["zones"]["dg"]["lag_s"] >= 40
    assert art["zones"]["eg"]["lag_s"] <= 40


def test_estimate_lags_without_raw_store(tmp_path: Any) -> None:
    """Kein lesbarer Roh-Store → alle Zonen Default-Lag, n=0, Artefakt ok."""
    art = rm.estimate_lags(data_dir=tmp_path / "leer", window_days=30,
                           now="2026-11-06 00:00:00",
                           out_path=tmp_path / "lag.json")
    assert art["n_samples_total"] == 0
    for z in list(config.ZONES) + ["central"]:
        zz = art["zones"][z]
        assert zz["lag_s"] == config.RM_DEFAULT_LAG_S
        assert zz["source"] == "default"
        assert zz["n"] == 0
        assert zz["confidence"] == 0.0
    assert (tmp_path / "lag.json").exists()


def test_load_lag_estimate_malformed(tmp_path: Any) -> None:
    """Ungültiges/fehlendes Artefakt → None (fail-safe)."""
    assert rm.load_lag_estimate(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ nicht-json")
    assert rm.load_lag_estimate(bad) is None
    no_zones = tmp_path / "no_zones.json"
    no_zones.write_text("{}")
    assert rm.load_lag_estimate(no_zones) is None


def test_to_grid_and_dt_raw_correction(tmp_path: Any) -> None:
    """``_to_grid`` (forward-fill) + ``_dt_raw_lag_corrected``-Arithmetik."""
    n = 100
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl = vl - 4.0  # konstanter ΔT = 4 K
    win = rm.RawWindow(ts=ts, vl=vl,
                       rl={"kell": rl, "eg": rl, "dg": rl, "central": rl},
                       rm={"kell": np.full(n, 10.0), "eg": np.full(n, 10.0),
                           "dg": np.full(n, 10.0)},
                       pump=np.ones(n), n=n)
    # ΔT (lag 0) = VL − RL = +4,0 K exakt.
    assert rm._dt_raw_lag_corrected(win, "kell", 0) == pytest.approx(4.0, abs=1e-6)
    # Lag-Versatz: RL(t+3) = VL(t+3) − 4 → ΔT(t, Lag 30 s) = VL(t) − (VL(t+3) − 4)
    # = 4 − (VL(t+3) − VL(t)) ≈ 4 (klein bei 600-s-Periode).
    assert rm._dt_raw_lag_corrected(win, "kell", 30) == pytest.approx(4.0, abs=0.3)
    # _to_grid: hold-last → Grid-Länge = (span/dt_s)+1, endliche Rohwerte
    # bleiben (kein Resampling-Drift); Werte vor dem ersten Sample = NaN.
    _, grid = rm._to_grid(ts, {"vl": vl}, 10)
    expected_len = int((ts[-1] - ts[0]).astype("int64") // 10) + 1
    assert grid["vl"].size == expected_len
    assert np.isfinite(grid["vl"]).all()
    assert grid["vl"][0] == pytest.approx(vl[0], abs=1e-6)
    # unbekannte Zone → None
    win2 = rm.RawWindow(ts=ts, vl=vl, rl={}, rm={}, pump=np.ones(n), n=n)
    assert rm._dt_raw_lag_corrected(win2, "kell", 0) is None


# --- 3) ΔT-Korrekt (pro-Call + Store-Fallback + Referenz-Beleg) --------------


def test_compute_diagnostics_raw_active() -> None:
    """Aktiv + Raw-Fenster → ΔT lag-korrigiert (VL − RL = +6 K), Ziel-Gap 4 K."""
    n = 100
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl = vl - 6.0
    win = rm.RawWindow(ts=ts, vl=vl,
                       rl={"kell": rl, "eg": rl, "dg": rl, "central": rl},
                       rm={"kell": np.full(n, 10.0), "eg": np.full(n, 10.0),
                           "dg": np.full(n, 10.0)},
                       pump=np.ones(n), n=n)
    art = {"zones": {"kell": {"lag_s": 0, "confidence": 0.7, "source": "estimate"},
                     "eg": {"lag_s": 0, "confidence": 0.7, "source": "estimate"},
                     "dg": {"lag_s": 0, "confidence": 0.7, "source": "estimate"}}}
    d = rm.compute_diagnostics(_row(), lag_art=art, raw=win)
    assert d.active is True
    for z in ("kell", "eg", "dg"):
        zz = d.per_zone[z]
        assert zz["delta_t_c"] == pytest.approx(6.0, abs=0.1)
        assert zz["delta_t_gap_c"] == pytest.approx(config.RM_DELTA_T_TARGET_C - 6.0, abs=0.1)
        assert zz["flow_class"] == "offen"
    jd = d.to_dict()
    assert jd["active"] is True
    assert set(jd["per_zone"]) == set(config.ZONES)
    assert jd["delta_t_target_c"] == config.RM_DELTA_T_TARGET_C
    assert jd["lag_source"] == {"kell": "estimate", "eg": "estimate", "dg": "estimate"}


def test_compute_diagnostics_inactive_stumm() -> None:
    """Inaktiv → per_zone stumm (leer im JSON), Kondition inaktiv."""
    n = 100
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl = vl - 6.0
    win = rm.RawWindow(ts=ts, vl=vl,
                       rl={"kell": rl, "eg": rl, "dg": rl, "central": rl},
                       rm={"kell": np.full(n, 0.0), "eg": np.full(n, 0.0),
                           "dg": np.full(n, 0.0)},
                       pump=np.zeros(n), n=n)
    rows = _row()
    rows.update({"pump_hz_on_sum3h": 0.0,
                 "hzrkl_kell_on": 0.0, "hzrkl_kell_on_lag1": 0.0,
                 "hzrkl_eg_on": 0.0, "hzrkl_eg_on_lag1": 0.0,
                 "hzrkl_dg_on": 0.0, "hzrkl_dg_on_lag1": 0.0,
                 "ras_kell_perc": 20.0, "ras_eg_perc": 20.0, "ras_dg_perc": 20.0,
                 "offset_kell": 0.0, "offset_eg": 0.0, "offset_dg": 0.0})
    d = rm.compute_diagnostics(rows, lag_art=None, raw=win)
    assert d.active is False
    jd = d.to_dict()
    assert jd["per_zone"] == {}  # Block stumm (maintainer-Bedingung)
    assert jd["active"] is False


def test_compute_diagnostics_store_fallback_dt() -> None:
    """Kein Raw-Fenster → stündlicher Store-2-h-Ø (hz_vl − hzrkl_{z}_pos)."""
    rows = _row()
    rows["hz_vl"] = 42.0
    rows["hzrkl_kell_pos"] = 32.0
    rows["hzrkl_kell_pos_last"] = 31.5
    rows["hz_vl_lag1"] = 41.0
    d = rm.compute_diagnostics(rows, lag_art=None, raw=None)
    # ΔT kell = Ø(42 − 32, 41 − 31,5) = Ø(10,0; 9,5) = 9,75 K
    assert d.per_zone["kell"]["delta_t_c"] == pytest.approx(9.75, abs=1e-6)
    assert d.per_zone["kell"]["n_raw_samples"] == 0
    assert d.per_zone["kell"]["lag_source"] == "default"


def test_compute_diagnostics_dt_none_when_missing() -> None:
    """Store-Spalten NULL → ΔT None (kein Crash)."""
    rows = _row()
    rows["hz_vl"] = None
    rows["hzrkl_kell_pos"] = None
    rows["hzrkl_kell_pos_last"] = None
    rows["hz_vl_lag1"] = None
    d = rm.compute_diagnostics(rows, lag_art=None, raw=None)
    assert d.per_zone["kell"]["delta_t_c"] is None
    assert d.per_zone["kell"]["delta_t_gap_c"] is None


def test_flow_class_klassen() -> None:
    """Durchfluss-Klassen: offen (≥ 0,5) / zu (≤ 0,1) / flappend (dazwischen)."""
    rows = _row()
    rows["hzrkl_eg_on"] = 0.2
    rows["hzrkl_eg_on_lag1"] = 0.2
    rows["hzrkl_dg_on"] = 0.0
    rows["hzrkl_dg_on_lag1"] = 0.0
    d = rm.compute_diagnostics(rows, lag_art=None, raw=None)
    assert d.per_zone["kell"]["flow_class"] == "offen"
    assert d.per_zone["eg"]["flow_class"] == "flappend"
    assert d.per_zone["dg"]["flow_class"] == "zu"


def test_rm_diagnostics_defaults() -> None:
    """Default-Diagnose (kein Artefakt): alle Zonen Default-Lag, conf 0."""
    defaults = config.rm_diagnostics_defaults()
    assert set(defaults) == set(config.ZONES)
    for z in config.ZONES:
        assert defaults[z]["lag_s"] == config.RM_DEFAULT_LAG_S
        assert defaults[z]["confidence"] == 0.0
        assert defaults[z]["source"] == "default"
        assert defaults[z]["delta_t_target_c"] == config.RM_DELTA_T_TARGET_C


# --- ΔT-Beleg gegen Referenz (547-h-Fenster 15.10.–07.11., mild) -------------


def test_referenz_dt_oeffnung_methode_reproduzierbar(tmp_path: Any) -> None:
    """Die ΔT-Bewertungsmethode (Hourly-Mean-ΔT je Minderer-offen-Klasse,
    Lag-korrigiert) reproduziert die Referenz-Beleg-Zahl: EG offen ≈ 4,1 K
    (n≈360), Keller zu ≈ 10,6 K (n≈531), DG offen ≈ 6,3 K (n≈139).

    Beleg (CIR-Log Q-124 / Plan.md, 2026-11-07): 547-h-Fenster 15.10.–07.11.
    Die Methode ist in ``rm_diagnostics`` implementiert (pro-Call: 10-s-Roh-
    Fenster mit Lag-Korrektur; Beleg: stündliche Ø über aktive Stunden). Diese
    Test verifiziert die **Klassen-Logik** (offen vs. zu) + Größenordnung an
    einer synthetischen Reproduktion des Referenz-Szenarios (EG offen 4 K,
    Keller zu 10 K) — die exakte Referenz-Zahl selbst ist ein einmaliger
    Daten-Beleg, keine laufende Berechnung.
    """
    n = 600  # 100 h à 360 Samples (10 s)
    rng = np.random.default_rng(42)
    # EG: offen (ΔT ≈ 4,1 K wie Referenz), Keller: zu (ΔT ≈ 10,6 K), DG: offen
    # (ΔT ≈ 6,3 K). VL schwankt (Außen-T-Einfluss), RL folgt (Lag 30 s).
    vl = _sine_signal(n, amp=4.0, base=37.0) + rng.normal(0, 0.3, n)
    eg_on = np.full(n, 10.0)
    dg_on = np.full(n, 10.0)
    rl_eg = np.empty(n)
    rl_dg = np.empty(n)
    rl_kell = np.empty(n)
    lag = 3
    for i in range(n):
        src = i - lag
        vl_src = vl[src] if src >= 0 else vl[0]
        # ΔT offen (EG 4,1 K / DG 6,3 K) bzw. zu (Keller 10,6 K).
        if eg_on[i] > 5:
            rl_eg[i] = vl_src - 4.1
        if dg_on[i] > 5:
            rl_dg[i] = vl_src - 6.3
        rl_kell[i] = vl_src - 10.6
    # Stunden-Ø ΔT je Klasse (Methode: VL-Stunden-Ø − RL-Stunden-Ø, aktiviert
    # über Minderer-offen).
    def hourly_dt(rlz: np.ndarray) -> tuple[float, int]:
        per_hour: dict[int, list[float]] = {}
        for i in range(n):
            h = i // 360
            per_hour.setdefault(h, []).append(vl[i] - rlz[i])
        vals = [np.mean(v) for v in per_hour.values()]
        return float(np.mean(vals)), len(vals)
    dt_eg, n_eg = hourly_dt(rl_eg)
    dt_dg, n_dg = hourly_dt(rl_dg)
    dt_kell, n_kell = hourly_dt(rl_kell)
    # Größenordnung der Referenz (synthetisch exakt gesetzt → ±0,3 K):
    assert dt_eg == pytest.approx(4.1, abs=0.3)
    assert dt_dg == pytest.approx(6.3, abs=0.3)
    assert dt_kell == pytest.approx(10.6, abs=0.3)
    # Referenz-Abgleich: EG offen 4,1 K (n=360), Keller zu 10,6 K (n=531),
    # DG offen 6,3 K (n=139) — 10-K-Kriterium heute NICHT erfüllt (EG ~4 K).
    assert dt_eg < config.RM_DELTA_T_TARGET_C  # „Fluss zu schnell“ (maintainer)
    assert dt_dg < config.RM_DELTA_T_TARGET_C
    assert dt_kell > config.RM_DELTA_T_TARGET_C  # Keller inaktiv (zu)
    assert n_eg > 0 and n_dg > 0 and n_kell > 0


# --- Minderer-Sollspannung (v0.5-engine-rework: a17–19, statische Vorgabe) ----


def test_soll_v_median_tail_und_fallbacks() -> None:
    """``RawWindow.soll_v``: 30-min-Median über das Fenster-ENDE; Kanal
    fehlt/leer/NaN → Default 10,0 V; expliziter Default überschreibbar."""
    n = 4000  # ~11,1 h à 10 s (» 30-min-Tail)
    ts = _sine_ts(n)
    vl = _sine_signal(n)
    base = rm.RawWindow(ts=ts, vl=vl, rl={}, rm={}, pump=np.ones(n), n=n)
    # leeres Soll-Dict (Legacy-/Synthese-Fenster ohne a17–19) → Default:
    assert base.soll_v("kell") == config.RM_MINDERER_SOLL_DEFAULT_V
    assert base.soll_v("kell", default=7.5) == 7.5
    # konstante Sollspannung 8,0 V → Median 8,0:
    base.soll = {"kell": np.full(n, 8.0)}
    assert base.soll_v("kell") == pytest.approx(8.0, abs=1e-9)
    # Tail-Median: erster Teil 10,0 V, letzter 30-min-Teil 4,0 V → Median
    # des Tails = 4,0 (statische Vorgabe wurde runtergesetzt):
    arr = np.full(n, 10.0)
    arr[-180:] = 4.0  # 180 Samples × 10 s = 30 min
    base.soll = {"kell": arr}
    assert base.soll_v("kell") == pytest.approx(4.0, abs=1e-9)
    # NaN in der ganzen Zone → Default:
    base.soll = {"kell": np.full(n, np.nan)}
    assert base.soll_v("kell") == config.RM_MINDERER_SOLL_DEFAULT_V


def test_compute_diagnostics_minderer_soll_v_aus_raw() -> None:
    """``compute_diagnostics``: ``per_zone.minderer_soll_v`` = 30-min-Median
    aus dem Roh-Fenster (a17–19); fehlend → Default 10,0 V."""
    n = 2000
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl = vl - 6.0
    win = rm.RawWindow(ts=ts, vl=vl,
                       rl={"kell": rl, "eg": rl, "dg": rl, "central": rl},
                       rm={"kell": np.full(n, 10.0), "eg": np.full(n, 10.0),
                           "dg": np.full(n, 10.0)},
                       pump=np.ones(n), n=n)
    # Soll: kell 7,5 V (konstant), eg/dg fehlen → Default 10,0 V:
    win.soll = {"kell": np.full(n, 7.5)}
    d = rm.compute_diagnostics(_row(), lag_art=None, raw=win)
    assert d.per_zone["kell"]["minderer_soll_v"] == pytest.approx(7.5, abs=1e-6)
    assert d.per_zone["eg"]["minderer_soll_v"] == config.RM_MINDERER_SOLL_DEFAULT_V
    assert d.per_zone["dg"]["minderer_soll_v"] == config.RM_MINDERER_SOLL_DEFAULT_V
    # kein Raw-Fenster → Default 10,0 V:
    d2 = rm.compute_diagnostics(_row(), lag_art=None, raw=None)
    for z in config.ZONES:
        assert d2.per_zone[z]["minderer_soll_v"] == config.RM_MINDERER_SOLL_DEFAULT_V


def test_read_raw_since_soll_kanal(tmp_path: Any) -> None:
    """``_read_raw_since``: a17–19 aus ``cmi_log_pq`` (P181) werden geladen
    (``RawWindow.soll``); ``_store_columns``-Schema-Check je Store."""
    n = 400
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl_v = vl - 6.0
    so_v = np.full(n, 7.5)
    make_raw_parquet(
        tmp_path / "raw", vl, {"kell": rl_v, "dg": rl_v, "eg": rl_v, "central": rl_v},
        {"kell": np.full(n, 10.0), "eg": np.full(n, 10.0), "dg": np.full(n, 0.0)},
        np.ones(n), ts,
        soll={"kell": so_v, "eg": so_v, "dg": so_v},
        store="cmi_log_pq",
    )
    win = rm._read_raw_since(tmp_path / "raw", "2026-11-04 20:00:00",
                             "2026-11-06 00:00:00")
    assert win is not None and win.n > 0
    assert "kell" in win.soll and "eg" in win.soll and "dg" in win.soll
    assert win.soll_v("kell") == pytest.approx(7.5, abs=1e-6)
    # Schema-Check: existierender Store → Spalten-Menge (inkl. a17);
    # fehlender Store → None (Store wird übersprungen):
    avail = rm._store_columns(tmp_path / "raw" / "cmi_log_pq")
    assert avail is not None and "dev2_analog_17" in avail and "ts" in avail
    assert rm._store_columns(tmp_path / "raw" / "winsol_pq") is None


def test_misch_store_soll_null_cast(tmp_path: Any) -> None:
    """Schema-Mix (Real-Setup): ``winsol_pq`` OHNE a17–19 + ``cmi_log_pq``
    MIT a17–19 → fehlende Spalten werden pro Store NULL-gecastet
    (Union-Schema bleibt ``_raw_columns()``); der Soll-Median kommt aus dem
    Store mit Kanal. (Real: cmi_log_pq P181 trägt die Sollspannungen,
    winsol_pq (ältere Aufnahme) nicht.)"""
    n = 400
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl_v = vl - 6.0
    rm_v = {"kell": np.full(n, 10.0), "eg": np.full(n, 10.0), "dg": np.full(n, 10.0)}
    # winsol_pq: KEINE a17–19 (Schema-Teilmenge):
    make_raw_parquet(tmp_path / "raw", vl,
                     {"kell": rl_v, "dg": rl_v, "eg": rl_v, "central": rl_v},
                     rm_v, np.ones(n), ts, soll=None, store="winsol_pq")
    # cmi_log_pq: MIT a17–19 (konstant 5,0 V):
    make_raw_parquet(tmp_path / "raw", vl,
                     {"kell": rl_v, "dg": rl_v, "eg": rl_v, "central": rl_v},
                     rm_v, np.ones(n), ts,
                     soll={"kell": np.full(n, 5.0), "eg": np.full(n, 5.0),
                           "dg": np.full(n, 5.0)},
                     store="cmi_log_pq")
    win = rm._read_raw_since(tmp_path / "raw", "2026-11-04 20:00:00",
                             "2026-11-06 00:00:00")
    assert win is not None and win.n > 0
    for z in config.ZONES:
        assert z in win.soll
        # Median über beide Stores (winsol-Teile NULL, cmi_log-Teile 5,0 V):
        assert win.soll_v(z) == pytest.approx(5.0, abs=1e-6)
    # Nur winsol_pq (ohne Kanal) → Soll-Arrays NULL → Default 10,0 V:
    (tmp_path / "raw2" / "winsol_pq").mkdir(parents=True)
    shutil.copytree(tmp_path / "raw" / "winsol_pq",
                    tmp_path / "raw2" / "winsol_pq", dirs_exist_ok=True)
    win2 = rm._read_raw_since(tmp_path / "raw2", "2026-11-04 20:00:00",
                              "2026-11-06 00:00:00")
    assert win2 is not None
    for z in config.ZONES:
        assert win2.soll_v(z) == config.RM_MINDERER_SOLL_DEFAULT_V


def test_read_raw_since_store_without_soll_columns(tmp_path: Any) -> None:
    """Store nur mit a10–12 (kein a17–19, s. echtes ``winsol_pq``) →
    ``RawWindow.soll`` = NULL-Arrays (Default 10,0 V), VL/RL/Pumpe intakt."""
    n = 400
    ts = _sine_ts(n)
    vl = _sine_signal(n, base=40.0)
    rl_v = vl - 6.0
    make_raw_parquet(tmp_path / "raw", vl,
                     {"kell": rl_v, "dg": rl_v, "eg": rl_v, "central": rl_v},
                     {"kell": np.full(n, 10.0), "eg": np.full(n, 10.0),
                      "dg": np.full(n, 10.0)},
                     np.ones(n), ts, soll=None, store="winsol_pq")
    win = rm._read_raw_since(tmp_path / "raw", "2026-11-04 20:00:00",
                             "2026-11-06 00:00:00")
    assert win is not None and win.n > 0
    assert win.soll_v("kell") == config.RM_MINDERER_SOLL_DEFAULT_V
    # ΔT-Arithmetik bleibt intakt (unabhängig vom Soll-Kanal):
    assert rm._dt_raw_lag_corrected(win, "kell", 0) == pytest.approx(6.0, abs=0.1)


# --- 4) Integration (recommend → JSON) ---------------------------------------


def _p_eq() -> FakePredictorV3:
    return FakePredictorV3(perz_base={"kell": 20.0, "eg": 20.0, "dg": 20.0},
                           gain={"kell": 0.0, "eg": 0.0, "dg": 0.0})


def test_recommend_json_rm_diagnostics_feld(tmp_path: Any, wx_ok: Any,
                                             skill_map: Any) -> None:
    """``recommend()`` → JSON trägt ``rm_diagnostics`` (aktiv, 3 Zonen,
    ΔT + Lag + target 10 K) + ``model_version l2-v005``."""
    root = make_mini_store(tmp_path / "features")
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(),
                    skill_map=skill_map)
    assert rec.model_version == "cmi-brain-v003+l2-v005"
    j = rec.to_json()
    assert "rm_diagnostics" in j
    d = j["rm_diagnostics"]
    assert d is not None
    assert d["active"] is True
    assert d["delta_t_target_c"] == config.RM_DELTA_T_TARGET_C
    assert set(d["per_zone"]) == set(config.ZONES)
    for z in config.ZONES:
        zz = d["per_zone"][z]
        for key in ("lag_s", "lag_confidence", "lag_source", "delta_t_c",
                    "delta_t_target_c", "delta_t_gap_c", "flow_class",
                    "rm_open_2h", "minderer_soll_v", "n_raw_samples"):
            assert key in zz, key
        # Kein Roh-Fenster im Test-Store → Default-Sollspannung 10,0 V:
        assert zz["minderer_soll_v"] == config.RM_MINDERER_SOLL_DEFAULT_V
    assert d["condition"]["active"] is True
    # deterministisch: zweiter Lauf → identisch.
    rec2 = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(),
                     skill_map=skill_map)
    assert rec2.to_json() == j


def test_recommend_rm_diagnostics_inactive_stumm(tmp_path: Any, wx_ok: Any,
                                                 skill_map: Any,
                                                 monkeypatch: Any) -> None:
    """Inaktiv (alle Minderer zu, RAS am Intent, Pumpe aus) → per_zone stumm.

    ``load_raw_window`` wird ge-mockt (keine Rohdaten im Test)."""
    root = make_mini_store(tmp_path / "features")
    import pyarrow as pa
    import pyarrow.parquet as pq

    p = root / "2026" / "11" / "2026-11.parquet"
    t = pq.read_table(p)
    for z in ("kell", "eg", "dg"):
        t = t.set_column(t.schema.get_field_index(f"hzrkl_{z}_on"),
                         pa.field(f"hzrkl_{z}_on", pa.float64()),
                         pa.array([0.0] * N_ROWS, type=pa.float64()))
        t = t.set_column(t.schema.get_field_index(f"hzrkl_{z}_on_lag1"),
                         pa.field(f"hzrkl_{z}_on_lag1", pa.float64()),
                         pa.array([0.0] * N_ROWS, type=pa.float64()))
        t = t.set_column(t.schema.get_field_index(f"ras_{z}_perc"),
                         pa.field(f"ras_{z}_perc", pa.float64()),
                         pa.array([20.0] * N_ROWS, type=pa.float64()))
        t = t.set_column(t.schema.get_field_index(f"offset_{z}"),
                         pa.field(f"offset_{z}", pa.float64()),
                         pa.array([0.0] * N_ROWS, type=pa.float64()))
    t = t.set_column(t.schema.get_field_index("pump_hz_on_sum3h"),
                     pa.field("pump_hz_on_sum3h", pa.float64()),
                     pa.array([0.0] * N_ROWS, type=pa.float64()))
    pq.write_table(t, p)

    def fake_raw(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("analysis.model.l2.recommend.load_raw_window", fake_raw)
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(),
                    skill_map=skill_map)
    d = rec.to_json()["rm_diagnostics"]
    assert d["active"] is False
    assert d["per_zone"] == {}  # Block stumm


def test_lag_estimate_wrapper_fallback(tmp_path: Any, monkeypatch: Any) -> None:
    """``load_lag_estimate(features_dir=…)``: kein Artefakt → None (Defaults
    werden in compute_diagnostics verwendet)."""
    root = make_mini_store(tmp_path / "features")
    # kein rm_lag_estimate.json im tmp → None
    assert load_lag_estimate(features_dir=root) is None
    # expliziter Pfad (ungültig) → None
    assert load_lag_estimate(features_dir=root, path=tmp_path / "none.json") is None


def test_load_raw_window_ohne_rohdaten(tmp_path: Any, monkeypatch: Any) -> None:
    """``load_raw_window`` ohne Roh-Store → None (kein Crash, Fallback)."""
    from analysis.model.l2 import recommend as rec_mod

    root = make_mini_store(tmp_path / "features")
    win = rec_mod.load_raw_window(features_dir=root)
    assert win is None  # kein winsol_pq/cmi_log_pq unter tmp → None


def test_recommend_deterministic_rm(tmp_path: Any, wx_ok: Any, skill_map: Any,
                                    monkeypatch: Any) -> None:
    """Zwei Läufe → identisches rm_diagnostics-JSON (kein RNG/Zeit-Drift)."""
    root = make_mini_store(tmp_path / "features")

    def fake_raw(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("analysis.model.l2.recommend.load_raw_window", fake_raw)
    r1 = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(),
                   skill_map=skill_map)
    r2 = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(),
                   skill_map=skill_map)
    assert r1.to_json()["rm_diagnostics"] == r2.to_json()["rm_diagnostics"]


def test_stale_recommendation_rm_diagnostics_none(tmp_path: Any, wx_old: Any,
                                                 skill_map: Any) -> None:
    """Stale-Pfad: Recommendation (stale) → rm_diagnostics = None, JSON
    serialisiert (Schema-Stabilität: neues Feld existiert immer)."""
    root = make_mini_store(tmp_path / "features")
    rec = recommend("2026-11-01 17:00", features_dir=root, wx=wx_old,
                    predictor=_p_eq(), skill_map=skill_map)
    assert rec.stale is True
    assert rec.rm_diagnostics is None
    j = rec.to_json()
    assert j["rm_diagnostics"] is None
    json.dumps(j)  # serialisierbar
