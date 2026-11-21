"""L2 v0.6-A — Minderer-V→ΔT-Kennlinie (2-Faktor, last-normalisiert).

Tests für :mod:`analysis.model.l2.minderer_curve` (Phasen-Erkennung,
2-Faktor-Fit, Sättigungs-Regime-Prüfung, Inverse + Guards, Cache) und den
datenbasierten Minderer-Zweig in
:func:`analysis.model.l2.recommend._minderer_advisories`:

- **Fit-Exaktheit**: synthetische Phasen aus ``ΔT = 40/V + 0,5·(ext − 5) +
  0,3`` (±kleines Rauschen, fester Seed) → Fit-Koeffizienten im
  Toleranz-Band.
- **Phasen-Erkennung**: konstante a18-Segmente (≥ 1 h) vs. zu-kurze
  Segmente (16 min → ausgeschlossen) vs. Phasen mit O8-AUS-Anteil
  (O8-AUS-Samples dürfen **nicht** in ``dT_offen``).
- **Inverse**: ``V(ΔT_ziel=10, ext)`` aus bekanntem a,b,c → exakt;
  Nenner-Guards (ext sehr hoch → V_MAX; V-Clip; ``min(V, v_now)``-Klemme).
- **Sättigungs-Regime**: 10-V-Punkt als Ausreißer →
  ``out_of_throttle_regime=True`` + Refit ohne 10-V (Inverse nutzt
  Drossel-Fit) + Kontext-Flag.
- **Fallback-Regression**: kein Cache / 0–1 Punkte / ``usable=False`` →
  linear-altes Verhalten **wortgleich** (gegen die aktuelle
  ``minderer_v_target_v``-Formel getestet).
- **Cache-Serialisierung** (JSON round-trip, ``usable``-Flag,
  ``ext_min/max``) + **Determinismus** (zweimal bauen = gleiche
  Koeffizienten).

Synthetische Stores/Artefakte, kein Live-Netz, kein CMI-Zugriff,
deterministisch (RNG nur mit festem Seed).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.l2 import config
from analysis.model.l2 import minderer_curve as mcurve
from analysis.model.l2.recommend import minderer_v_target_v, tier_b_advisories
from analysis.model.l2.tests.test_v005_tickets_abc import (
    _diag,
    _minderer_block,
    _real_discovery,
    _state,
)

# --- Synthetische Phase-Serie (10-s-Raster) ---------------------------------


def _phase_ts(t0_s: int, dur_s: int) -> np.ndarray:
    """10-s-Stampfen über ``dur_s`` Sekunden ab ``t0_s`` (Epoch-Basis)."""
    n = dur_s // 10
    sec = (np.arange(n) * 10 + t0_s).astype("int64")
    return np.asarray(sec, dtype="datetime64[s]")


# „1-h-Phase“ = 3610 s (361 Samples, Spanne 3600 s — exakt 1 h; 360 Samples
# wären 3590 s < 1 h → die Phase würde die Mindestdauer verfehlen).
PHASE_1H_S = 3610


def make_phase_series(
    specs: list[tuple[int, float, float]],
    ext: float = 5.0,
    vl: float = 40.0,
    rl_base: float = 30.0,
    normal_soll: float = 18.0,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Konkatene Phasen-Serie (10 s-Raster).

    ``specs`` = ``[(t0_s, soll_v, dt_offen), …]``. Je Phase: ``soll`` =
    konstante Sollspannung, ``vl = rl_base + dt_offen`` (konstant),
    ``ext``/``raum_soll``/``pump=1``/``minder_sig = soll`` (An-Takt) konstant
    → jede Phase liefert exakt ``dt_offen`` als Kennlinien-Metrik.
    """
    ts_parts: list[np.ndarray] = []
    cols: dict[str, list[float]] = {
        "soll": [],
        "vl": [],
        "rl": [],
        "ext": [],
        "pump": [],
        "raum_soll": [],
        "sig": [],
    }
    for t0, soll_v, _dt in specs:
        ts = _phase_ts(t0, PHASE_1H_S)
        n = ts.size
        ts_parts.append(ts)
        cols["soll"] += [soll_v] * n
        cols["vl"] += [vl] * n
        cols["rl"] += [rl_base] * n
        cols["ext"] += [ext] * n
        cols["pump"] += [1.0] * n
        cols["raum_soll"] += [normal_soll] * n
        cols["sig"] += [soll_v] * n
    arr = {k: np.array(v, dtype=np.float64) for k, v in cols.items()}
    return np.concatenate(ts_parts), arr


# --- 1) Fit-Exaktheit ---------------------------------------------------------


def test_fit_2factor_exakt_rauschfrei() -> None:
    """Synthetische Phasen aus ``ΔT = 40/V + 0,5·(ext−5) + 0,3`` (rauschfrei)
    → Fit-Koeffizienten = exakt: a = 40, b = 0,5, und der Offset-Term bei
    ext = 5 °C (``b·5 + c``) = 0,3 (die Task-Formel ist bei ext = 5 °C
    last-normalisiert → Modell-``c = 0,3 − 0,5·5 = −2,2``)."""
    pts = []
    for v, ext in [(2.0, 4.0), (5.0, 6.0), (7.0, 8.0), (10.0, 5.0)]:
        dt = 40.0 / v + 0.5 * (ext - 5.0) + 0.3
        pts.append((v, ext, dt))
    fit = mcurve.fit_2factor(pts)
    assert fit["a"] == pytest.approx(40.0, abs=1e-6)
    assert fit["b"] == pytest.approx(0.5, abs=1e-6)
    assert fit["b"] * 5.0 + fit["c"] == pytest.approx(0.3, abs=1e-6)
    assert fit["resid_sigma"] == pytest.approx(0.0, abs=1e-6)


def test_fit_2factor_rauschen_fester_seed() -> None:
    """±0,15-K-Rauschen (fester Seed) → Koeffizienten im Toleranz-Band
    (a ±1 K·V, b ±0,05, Offset-Term ±1 K) + σ > 0 (MSE-basiert)."""
    rng = np.random.default_rng(42)
    pts = []
    for v, ext in [(2.0, 4.0), (5.0, 6.0), (7.0, 8.0), (10.0, 5.0), (3.0, 7.0)]:
        dt = 40.0 / v + 0.5 * (ext - 5.0) + 0.3 + float(rng.uniform(-0.15, 0.15))
        pts.append((v, ext, dt))
    fit = mcurve.fit_2factor(pts)
    assert fit["a"] == pytest.approx(40.0, abs=1.0)
    assert fit["b"] == pytest.approx(0.5, abs=0.05)
    assert fit["b"] * 5.0 + fit["c"] == pytest.approx(0.3, abs=1.0)
    assert fit["resid_sigma"] is not None and fit["resid_sigma"] > 0.0


def test_fit_2factor_zwei_punkte() -> None:
    """Exakter 2-Punkt-Fit (c = 0-Referenz, σ = n/a = None):
    2 Punkte mit gleicher ext → a exakt, b unbestimmt (0,0)."""
    # ΔT = 40/V + 0,5·(ext−5) + 0,3 bei ext = 5,0 → ΔT = 40/V + 0,3.
    p1 = (2.0, 5.0, 40.0 / 2.0 + 0.3)
    p2 = (5.0, 5.0, 40.0 / 5.0 + 0.3)
    fit = mcurve.fit_2factor([p1, p2])
    assert fit["a"] == pytest.approx(40.0, abs=1e-6)
    assert fit["c"] == 0.0
    assert fit["resid_sigma"] is None
    # Degeneriert (identische Feature-Kombination) → 0,0-Fit (kein Crash):
    deg = mcurve.fit_2factor([(2.0, 5.0, 10.0), (2.0, 5.0, 10.0)])
    assert deg["a"] == 0.0 and deg["b"] == 0.0 and deg["c"] == 0.0


# --- 2) Phasen-Erkennung -------------------------------------------------------


def test_detect_phases_min_deuer_exklusiv() -> None:
    """Mindestdauer ≥ 1 h: die 16-min-1-V-Phase (analog der 07.11-Phase)
    wird **ausgeschlossen**; 1-h-Phasen bleiben."""
    ts, _ = make_phase_series([(0, 1.0, 14.0), (3610, 10.0, 8.0)])
    # 16-min-1-V-Phase (960 s) + 2-h-10-V-Phase:
    ts_short = np.concatenate([
        _phase_ts(0, 960),
        _phase_ts(3600, 7200),
    ])
    soll_short = np.concatenate([np.full(96, 1.0), np.full(720, 10.0)])
    phases = mcurve.detect_phases(ts_short, soll_short)
    assert len(phases) == 1
    assert phases[0]["v"] == 10.0
    assert phases[0]["n"] == 720
    # Zwei 1-h-Phasen (1 V + 10 V) zählen beide (Mindestdauer erfüllt):
    soll_2h = np.concatenate([np.full(361, 1.0), np.full(361, 10.0)])
    phases2 = mcurve.detect_phases(ts, soll_2h)
    assert [p["v"] for p in phases2] == [1.0, 10.0]
    assert all(p["n"] == 361 for p in phases2)


def test_detect_phases_null_luecke_bricht_segment() -> None:
    """NULL-Lücke (Logger-Aussetzer) > max-Gap bricht das Segment: die
    zweite (1-h-)Strecke ist komplett NULL → fällt raus; nur das erste
    1-h-Segment bleibt."""
    ts1, _ = make_phase_series([(0, 10.0, 8.0)])
    ts2, _ = make_phase_series([(3610, 10.0, 8.0)])
    ts = np.concatenate([ts1, ts2])
    soll = np.concatenate([np.full(361, 10.0), np.full(361, np.nan)])
    phases = mcurve.detect_phases(ts, soll)
    assert [p["v"] for p in phases] == [10.0]
    assert phases[0]["i1"] - phases[0]["i0"] + 1 == 361


def test_detect_phases_leer_kurze_input() -> None:
    """Keine Phasen bei leerem/NULL-Signal oder < 2 Samples."""
    ts = np.arange(5, dtype="int64").astype("datetime64[s]")
    assert mcurve.detect_phases(ts, np.array([np.nan] * 5)) == []
    assert mcurve.detect_phases(ts[:1], np.array([10.0])) == []


# --- 3) Phase-Metriken (O8-AN + NORMAL + Minderer-offen) ---------------------


def test_phase_metrics_o8_aus_und_absenk_ausgeschlossen() -> None:
    """O8-AUS-Samples (pump = 0) und ABSENK-Samples (raum_soll = 16)
    dürfen **nicht** in ``dT_offen`` (Methode: Pump-ON-Flow-Only)."""
    ts, arr = make_phase_series([(0, 5.0, 9.35)])
    n = ts.size
    # Erste Hälfte: O8 AUS (pump = 0) → ausblendbar; zweites Viertel:
    # ABSENK (raum_soll = 16) → ausblendbar; Rest = Kennlinien-Samples.
    h = n // 2
    q = n // 4
    pump = arr["pump"].copy()
    pump[:h] = 0.0
    rs = arr["raum_soll"].copy()
    rs[h : h + q] = 16.0  # ABSENK (NORMAL = ≥ 18)
    sig = arr["sig"].copy()
    sig[:q] = 0.0  # Minderer-Zu-Takt (≤ 0,1 V → ausblenden)
    ph = {"v": 5.0, "i0": 0, "i1": n - 1, "n": n}
    met = mcurve.phase_metrics(
        ph, np.full(n, 44.0), np.full(n, 34.65), np.full(n, 5.5),
        pump, rs, sig,
    )
    # Erwartete Anzahl = O8-AN + NORMAL + Minderer-offen-Intersection (direkt
    # aus den Masken berechnet, statt von Hand):
    expected = int(
        np.sum((pump == 1.0) & (rs >= 18.0) & (sig > 0.1))
    )
    assert met is not None
    assert met["n"] == expected
    # O8-AUS/ABSENK/Zu-Samples sind tatsächlich ausgeschlossen (expected < n):
    assert expected < n
    assert met["dt_offen"] == pytest.approx(44.0 - 34.65, abs=1e-6)
    assert met["aussen_o"] == pytest.approx(5.5, abs=1e-6)
    assert met["v"] == 5.0
    assert met["dt_offen"] == pytest.approx(44.0 - 34.65, abs=1e-6)
    assert met["aussen_o"] == pytest.approx(5.5, abs=1e-6)
    assert met["v"] == 5.0


def test_phase_metrics_zu_wenig_samples_none() -> None:
    """< 30 Kennlinien-Samples → ``None`` (keine belastbare Phase-Metrik)."""
    ts, arr = make_phase_series([(0, 5.0, 9.0)])
    pump = np.zeros(ts.size)  # O8 AUS durchgehend
    ph = {"v": 5.0, "i0": 0, "i1": ts.size - 1, "n": ts.size}
    met = mcurve.phase_metrics(
        ph, np.full(ts.size, 44.0), np.full(ts.size, 35.0), np.full(ts.size, 5.0),
        pump, np.full(ts.size, 18.0), arr["sig"],
    )
    assert met is None


# --- 4) Inverse + Guards ------------------------------------------------------


def test_v_inverse_exakt_aus_bekanntem_fit() -> None:
    """V(ΔT_ziel=10, ext) aus bekanntem a,b,c (2-Faktor-Modell) → exakt.

    Modell: ΔT = 30/V − 0,4·ext + 3,0. ΔT_ziel = 10, ext = 5 →
    Nenner = 10 − (−0,4·5 + 3,0) = 10 − 1,0 = 9,0 → V = 30/9 ≈ 3,33 V."""
    curve = {"a": 30.0, "b": -0.4, "c": 3.0, "usable": True}
    v = minderer_v_target_v(6.0, 10.0, ext_c=5.0, curve=curve)
    assert v == pytest.approx(30.0 / 9.0, abs=1e-9)
    # Klemme min(V, v_now): v_now = 3,0 < 3,33 → 3,0 V (nie über die
    # aktuelle Vorgabe hinaus öffnen).
    v2 = minderer_v_target_v(6.0, 3.0, ext_c=5.0, curve=curve)
    assert v2 == pytest.approx(3.0, abs=1e-9)


def test_v_inverse_nenner_guard_v_max() -> None:
    """Nenner ≤ 0,5 K → V_MAX (ext sehr hoch: Lastterm übersteigt Ziel —
    nicht per Minderer erreichbar → öffnen/beibehalten)."""
    curve = {"a": 30.0, "b": 0.5, "c": 2.0, "usable": True}
    # ext = 30 → Nenner = 10 − (0,5·30 + 2,0) = −7,0 → V_MAX.
    v = minderer_v_target_v(10.0, 2.0, ext_c=30.0, curve=curve)
    assert v == config.RM_V_MAX
    # ext = 14 → Nenner = 10 − (7,0 + 2,0) = 1,0 > 0,5 → V = 30/1 = 30 →
    # Clip 10 V.
    v2 = minderer_v_target_v(10.0, 10.0, ext_c=14.0, curve=curve)
    assert v2 == config.RM_V_MAX


def test_v_inverse_undefined_target() -> None:
    """ΔT > Ziel + Toleranz (Fluss langsam → öffnen): der Advisory-Text
    (nicht die Inverse) entscheidet „in Richtung 10 V öffnen“; die
    Inverse liefert konservativ min(V, v_now). ΔT_ziel = 10, ext = 5,
    Modell wie oben → Nenner = 9,0 → V = 30/9 ≈ 3,33 → min(3,33, v_now)."""
    curve = {"a": 30.0, "b": -0.4, "c": 3.0, "usable": True}
    v = minderer_v_target_v(12.0, 10.0, ext_c=5.0, curve=curve)
    assert v == pytest.approx(30.0 / 9.0, abs=1e-9)


# --- 5) Fallback-Regression (linear altes Verhalten) -------------------------


def _linear_ref(delta_t_c: float, current_v: float) -> float:
    """v0.5-Linearschätzung (wortlich die alte Formel)."""
    tgt = config.RM_DELTA_T_TARGET_C
    current_v = float(np.clip(current_v, 0.0, config.RM_V_MAX))
    gap = tgt - float(delta_t_c)
    if gap <= 0.0:
        return config.RM_V_MAX
    close_frac = gap / tgt if tgt > 0.0 else 1.0
    return float(np.clip(current_v * (1.0 - close_frac), 0.0, config.RM_V_MAX))


@pytest.mark.parametrize(
    ("dt", "v_now"),
    [(0.0, 10.0), (4.0, 10.0), (6.0, 5.0), (8.0, 10.0), (10.0, 10.0),
     (15.0, 10.0), (-5.0, 10.0), (4.0, 10.5), (4.0, 5.0)],
)
def test_fallback_linear_wortgleich(dt: float, v_now: float) -> None:
    """Keine Kurve / keine Außen-T / nicht-usable → linear altes Verhalten
    **wortgleich** (gegen die v0.5-Formel)."""
    curve_unusable = {"a": 30.0, "b": -0.4, "c": 3.0, "usable": False}
    assert minderer_v_target_v(dt, v_now) == pytest.approx(_linear_ref(dt, v_now), abs=1e-9)
    # Kurve vorhanden, aber usable=False → Fallback:
    assert minderer_v_target_v(dt, v_now, ext_c=5.0, curve=curve_unusable) == \
        pytest.approx(_linear_ref(dt, v_now), abs=1e-9)
    # Kurve usable, aber ext_c=None (keine Außen-T) → Fallback:
    curve_ok = {"a": 30.0, "b": -0.4, "c": 3.0, "usable": True}
    assert minderer_v_target_v(dt, v_now, ext_c=None, curve=curve_ok) == \
        pytest.approx(_linear_ref(dt, v_now), abs=1e-9)


def test_recommend_minderer_block_ohne_cache_linear() -> None:
    """E2E (``_minderer_advisories``): KEIN Kurven-Cache → Advisory-Text
    bleibt „linear geschätzt“ (v0.5-Verhalten exakt, Regression)."""
    diag = _diag({"kell": 1.0, "eg": 0.0, "dg": 0.0},
                 {"kell": 5.0, "eg": 40.0, "dg": 40.0})
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    b = _minderer_block(adv)[0]
    assert "linear geschätzt" in b["vorgeschlagen"]
    assert b["konfidenz"] == 0.3
    # ΔT 5 K → V-Ziel = 10·(1 − 5/10) = 5,0 V (lineare Formel).
    assert "10.0 V → ~5.0 V" in b["vorgeschlagen"]


# --- 6) Cache + Determinismus -------------------------------------------------


def test_cache_serialisierung_roundtrip(tmp_path: Path) -> None:
    """JSON-Roundtrip: usables Zone-Fit (a/b/c, σ, ext_min/max, usable-Flag)
    + nicht-usable Zone (usable=False, kein a/b/c) → ``load``/``zone_entry``
    funktionieren."""
    art = {
        "generated_at": "2026-11-18 08:00:00",
        "learn_start": config.VL_MIN_LEARN_START,
        "method": "test",
        "zones": {
            "eg": {
                "n_points": 4,
                "usable": True,
                "fit_source": "drossel_regime_ohne_10v",
                "out_of_throttle_regime": True,
                "satur_10v_resid_k": 3.0,
                "a": 23.0,
                "b": -0.98,
                "c": 9.0,
                "resid_sigma": None,
                "ext_min": 4.4,
                "ext_max": 9.9,
                "residuals": [{"v": 2.0, "resid_k": 0.0}],
                "satur_10v": {"v": 10.0, "dt_offen": 8.08, "aussen_o": 6.4},
                "points": [],
            },
            "kell": {
                "n_points": 1,
                "usable": False,
                "fit_source": "all_points",
                "out_of_throttle_regime": False,
                "satur_10v_resid_k": None,
                "ext_min": 6.0,
                "ext_max": 6.0,
                "residuals": [],
                "satur_10v": None,
                "points": [],
            },
            "dg": {
                "n_points": 0,
                "usable": False,
                "fit_source": "none",
                "out_of_throttle_regime": False,
                "satur_10v_resid_k": None,
                "ext_min": None,
                "ext_max": None,
                "residuals": [],
                "satur_10v": None,
                "points": [],
            },
        },
        "constants": {},
    }
    p = tmp_path / "curve.json"
    p.write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")
    loaded = mcurve.load_minderer_curve(p)
    assert loaded is not None
    eg = mcurve.zone_entry(loaded, "eg")
    assert eg is not None and eg["usable"] is True
    assert eg["a"] == 23.0 and eg["b"] == -0.98 and eg["c"] == 9.0
    assert eg["ext_min"] == 4.4 and eg["ext_max"] == 9.9
    assert mcurve.zone_entry(loaded, "kell") is None  # nicht usable
    assert mcurve.zone_entry(loaded, "dg") is None  # 0 Punkte
    assert mcurve.zone_entry(None, "eg") is None  # kein Artefakt
    # OoR-Flag: ext 3,0 < ext_min 4,4 → True; ext 7,0 im Band → False;
    # ext = None / nicht-usable Zone → False.
    assert mcurve.ext_oob_flag(loaded, "eg", 3.0) is True
    assert mcurve.ext_oob_flag(loaded, "eg", 7.0) is False
    assert mcurve.ext_oob_flag(loaded, "eg", None) is False
    assert mcurve.ext_oob_flag(loaded, "kell", 3.0) is False
    # Pfad-Logik (Muster ``vl_minimum_path``):
    assert mcurve.minderer_curve_path() == config.RM_CURVE_ESTIMATE_PATH
    assert mcurve.minderer_curve_path(Path("/data")) == Path("/data/l2/minderer_curve.json")
    # Fehlerfälle:
    assert mcurve.load_minderer_curve(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ nicht-json", encoding="utf-8")
    assert mcurve.load_minderer_curve(bad) is None
    noz = tmp_path / "no_zones.json"
    noz.write_text("{}", encoding="utf-8")
    assert mcurve.load_minderer_curve(noz) is None


def _synth_raw_store(root: Path) -> Path:
    """Synthetischer Rohdaten-Store (Hive, ``cmi_log_pq``-Analog; ``ts``
    VARCHAR): EG (a18) = 4 Phasen (10/5/7/2 V) mit 10-V-Sättigungs-
    Ausreißer (dt 8,08 K, ext 6,4 °C — liegt deutlich über dem
    2/5/7-V-1/V-Trend → Sättigungs-Flag); kell (a17) + dg (a19) =
    durchgehend 10 V (je 1 Phase → usable=False, Fallback)."""
    specs_eg: list[tuple[int, float, float, float]] = [
        (0, 10.0, 8.08, 6.4),
        (3610, 5.0, 9.35, 4.4),
        (7220, 7.0, 5.5, 7.0),
        (10830, 2.0, 10.81, 10.0),
    ]
    # Zeitbasis: nach dem Lernstart (2026-11-07 16:14 UTC) — die
    # ``VL_MIN_LEARN_START``-WHERE-Pushdown würde 1970-Epochen-Phasen
    # andernfalls ausfiltern.
    base = int(np.datetime64("2026-11-07T16:20:00", "s").astype("int64"))
    ts_parts: list[np.ndarray] = []
    cols: dict[str, list[float]] = {
        "vl": [], "ext": [], "rs": [], "pump": [],
        "rl_eg": [], "sig_eg": [], "soll_eg": [],
    }
    for t0, soll, dt, ext in specs_eg:
        ts = _phase_ts(base + t0, PHASE_1H_S)
        n = ts.size
        ts_parts.append(ts)
        cols["vl"] += [40.0] * n
        cols["ext"] += [ext] * n
        cols["rs"] += [18.0] * n
        cols["pump"] += [1.0] * n
        cols["rl_eg"] += [40.0 - dt] * n
        cols["sig_eg"] += [soll] * n
        cols["soll_eg"] += [soll] * n
    ts_all = np.concatenate(ts_parts)
    n = ts_all.size
    tbl = {
        "ts": pa.array([str(t) for t in ts_all]),
        "dev0_analog_06": pa.array(cols["vl"], type=pa.float64()),
        "dev0_analog_08": pa.array(cols["ext"], type=pa.float64()),
        "dev0_analog_12": pa.array(cols["rs"], type=pa.float64()),
        "dev0_digital_08": pa.array(cols["pump"], type=pa.float64()),
        "dev2_analog_13": pa.array([31.0] * n, type=pa.float64()),
        "dev2_analog_14": pa.array([31.0] * n, type=pa.float64()),
        "dev2_analog_15": pa.array(cols["rl_eg"], type=pa.float64()),
        "dev2_analog_10": pa.array([10.0] * n, type=pa.float64()),
        "dev2_analog_11": pa.array(cols["sig_eg"], type=pa.float64()),
        "dev2_analog_12": pa.array([10.0] * n, type=pa.float64()),
        "dev2_analog_17": pa.array([10.0] * n, type=pa.float64()),
        "dev2_analog_18": pa.array(cols["soll_eg"], type=pa.float64()),
        # DG = 0-V-Phase (Minderer zu, kein O8-AN-Flow → kein Kennlinien-
        # Punkt) → NULL (kein Phasen-Material, n_points = 0):
        "dev2_analog_19": pa.array([None] * n, type=pa.float64()),
    }
    p = root / "cmi_log_pq" / "2026" / "11" / "2026-11.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(tbl), p)
    return root


def test_build_curve_deterministisch_und_sattigung(tmp_path: Path) -> None:
    """Build aus synthetischem Store: EG = 4 Phasen → usable, 10-V-Punkt
    außerhalb des Drossel-Regimes (Sättigungs-Flag + Refit ohne 10-V),
    kell/dg = 1 Phase → usable=False (Fallback). **Determinismus**: zweimal
    bauen = gleiche Koeffizienten. Cache-Persistenz + round-trip."""
    _synth_raw_store(tmp_path / "raw")
    art1 = mcurve.build_minderer_curve(data_dir=tmp_path / "raw",
                                       now="2026-11-18 08:00:00",
                                       out_path=tmp_path / "c1.json")
    art2 = mcurve.build_minderer_curve(data_dir=tmp_path / "raw",
                                       now="2026-11-18 09:00:00",
                                       out_path=tmp_path / "c2.json")
    # EG: 4 Phasen (10/5/7/2 V), usable, Sättigungs-Regime (10-V-Residuum
    # groß → Refit ohne 10-V):
    eg = art1["zones"]["eg"]
    assert eg["n_points"] == 4
    assert eg["usable"] is True
    assert eg["out_of_throttle_regime"] is True
    assert eg["fit_source"] == "drossel_regime_ohne_10v"
    assert eg["satur_10v"] is not None and eg["satur_10v"]["v"] == 10.0
    # Drossel-Fit (ohne 10-V): a/b/c aus 2/5/7-V-Punkten (last-normalisiert).
    # Die 2/5/7-V-Punkte sind NICHT exakt 1/V-kollinear (ext-variiert) →
    # Fit ist definiert; Koeffizienten deterministisch (zweimal gleich).
    assert eg["a"] == art2["zones"]["eg"]["a"]
    assert eg["b"] == art2["zones"]["eg"]["b"]
    assert eg["c"] == art2["zones"]["eg"]["c"]
    # ext-Band = Drossel-Regime (4,4…10,0 °C, OHNE dem 10-V-Punkt (6,4 °C
    # liegt im Band → Band bleibt 4,4…10,0)):
    assert eg["ext_min"] == 4.4 and eg["ext_max"] == 10.0
    # Residuen je Punkt (4 Punkte):
    assert len(eg["residuals"]) == 4
    resids = {r["v"]: r["resid_k"] for r in eg["residuals"]}
    assert set(resids) == {2.0, 5.0, 7.0, 10.0}
    # kell: 1 Phase (10 V, durchgehend) → usable=False; dg: 0-V-Phase (Minderer
    # zu, kein Flow) → 0 Phasen → usable=False:
    assert art1["zones"]["kell"]["usable"] is False
    assert art1["zones"]["kell"]["n_points"] == 1
    assert art1["zones"]["dg"]["usable"] is False
    assert art1["zones"]["dg"]["n_points"] == 0
    # Cache persistiert + round-trip:
    assert (tmp_path / "c1.json").exists()
    loaded = mcurve.load_minderer_curve(tmp_path / "c1.json")
    assert loaded is not None
    assert mcurve.zone_entry(loaded, "eg")["usable"] is True
    assert mcurve.zone_entry(loaded, "kell") is None
    assert mcurve.zone_entry(loaded, "dg") is None


def test_build_curve_fehlende_stores(tmp_path: Path) -> None:
    """Keine Rohdaten-Stores unter data_dir → ``FileNotFoundError``."""
    with pytest.raises(FileNotFoundError):
        mcurve.build_minderer_curve(data_dir=tmp_path / "leer",
                                    out_path=tmp_path / "c.json")


def test_recommend_minderer_block_datenbasiert() -> None:
    """E2E: usables EG-Fit + IST-Außen-T → Advisory-Text „datenbasiert
    (n Punkte, last-normalisiert)“ statt „linear geschätzt“; OoR-Flag bei
    Außen-T außerhalb des Kalibrierungsbands (kein Stopp)."""
    curve_art = {
        "zones": {
            "eg": {
                "n_points": 4,
                "usable": True,
                "a": 23.0,
                "b": -0.98,
                "c": 9.0,
                "resid_sigma": None,
                "ext_min": 4.4,
                "ext_max": 10.0,
                "out_of_throttle_regime": True,
            },
            "kell": {"n_points": 1, "usable": False},
            "dg": {"n_points": 0, "usable": False},
        }
    }
    # ΔT = 6 K < Ziel − Toleranz → drosseln; ext = 13,0 °C (OoR: Band
    # 4,4…10,0) → OoR-Text-Flag.
    diag = _diag({"kell": 0.0, "eg": 1.0, "dg": 0.0},
                 {"kell": 40.0, "eg": 6.0, "dg": 40.0}, soll_v=5.0)
    st = _state(ext=13.0)
    adv = tier_b_advisories(st, None, [], _real_discovery(), rm_diag=diag,
                            minderer_curve=curve_art)
    b = _minderer_block(adv)[0]
    assert b["zone"] == "eg"
    assert "datenbasiert (4 Punkte, last-normalisiert)" in b["vorgeschlagen"]
    assert "Sättigung, Kontext" in b["vorgeschlagen"]
    assert "linear geschätzt" not in b["vorgeschlagen"]
    assert "Kalibrierungsbands 4.4…10.0 °C" in b["begruendung_de"]
    assert "Extrapolation" in b["begruendung_de"]
    # Inverse: V = 23/(10 − (−0,98·13 + 9)) = 23/(10 + 3,74) = 23/13,74
    # ≈ 1,67 V → min(1,67, v_now=5) = 1,67 V:
    expected_v = 23.0 / (10.0 - (-0.98 * 13.0 + 9.0))
    assert f"~{expected_v:.1f} V" in b["vorgeschlagen"]
    # kell (nicht usable) → linear-Fallback-Text bleibt:
    # (kell ist inaktiv → kein Block; hier: nur eg aktiv.)
