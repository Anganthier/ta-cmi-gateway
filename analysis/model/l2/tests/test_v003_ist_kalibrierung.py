"""L2 v0.3 (FIX) — Komfort in Ist-Raum gegen den fixen Intent (20 − offset_now).

Regression-Tests für die maintainer-Korrektur 2026-11-02 + Orchestrator-Fix
2026-11-03 (docs/l2-v0.3-ist-kalibrierung.md): Komfort-Referenz = the maintainer's
**aktueller Intent** ``soll = RAS_BASE_C − offset_now`` (fix, KEIN Δ) — NICHT
das Kandidaten-eigene Ziel (v0.3-Alpha: zirkulär + h=0-Buchführungs-Artefakt).

1. **DG-Gleichgewichts-Regression:** perz ≈ 20 (Ist = 20 − Off, RAS regelt Perz
   auf 20) → keine Offset-Änderung (Δ in Band 0,5 → Komfort 0; Change 0,25 >
   No-change-Schwelle 0,15).
2. **Keller-Richtungs-Regression (Trigger-Fall, zu warm):** perz > 20,
   perz-haltend → **kein** Δ<0 (Soll rauf = wärmer) gewinnt; Δ=−0,5 strikt
   teurer als No-Change (Komfort allein); bester Kandidat Δ ≥ 0.
3. **Neu zu-kalt-Richtung:** perz haltend unter dem Intent → bester Δ ≤ 0
   (Soll runter = mehr Heizung); Δ>0 verliert strikt.
4. **h=0-Identität (Unit):** Abweichung bei h=0 = |ist_now − intent| für ALLE
   Δ (identisch); h>0 unterscheidet korrekt (perz-Transient).
5. **Ableitung + Anzeige:** intent = 20 − off_now (off +3,5 → 16,5 ≠ 20);
   JSON trägt „Ist … (Soll …, Abw. … K)" (vorzeichungsgetreu).
6. **model_version = cmi-brain-v003+l2-v004** (v0.4-Identität; v0.3-Baseline)
   + Determinismus.

Synthetic-Store + FakePredictor (kein Live-Netz, kein echtes L1-Artefakt).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from analysis.model.l2 import config
from analysis.model.l2.recommend import (
    OFFSET_STEP_COLS,
    ROLLOUT_TARGETS,
    candidate_cost,
    comfort_cost,
    load_state,
    ras_soll_now,
    recommend,
    rollout,
)
from analysis.model.l2.tests.conftest import N_ROWS, make_mini_store
from analysis.model.m2b.data import load_store

# Offsets des Mini-Stores (Default, unverändert): kell +3,5 / eg −1,8 / dg +1,4.
OFF = {"kell": 3.5, "eg": -1.8, "dg": 1.4}
TS = "2026-11-01 11:00"


def _set_cols(root: Any, **over: float) -> Any:
    """Spezielle Store-Spalten (Ist/Perzipiert) je Zeile überschreiben."""
    p = root / "2026" / "11" / "2026-11.parquet"
    t = pq.read_table(p)
    for name, val in over.items():
        arr = pa.array([val] * N_ROWS, type=pa.float64())
        t = t.set_column(t.schema.get_field_index(name), pa.field(name, pa.float64()), arr)
    pq.write_table(t, p)
    return root


class FakePredictorV3:
    """Deterministischer L1-Ersatz mit **korrekter** RAS-Rückkopplung.

    ``perz_cand(z) = perz_base[z] − gain[z]·Δz`` (h>0): Offset-RAUF (Δ>0)
    kühlt (perz Richtung 20), Offset-AB kühlt nicht. h=0 = geänderter
    Ist-Zustand (``perz_base + Δ``), wie in :func:`rollout` gesetzt.
    ``gain=0`` = Gleichgewichts-Zone (RAS hält perz am Basiswert,
    kandidatenunabhängig).
    """

    horizons: tuple[int, ...] = (1, 4, 6, 12)
    raw_inputs: list[str] = ["ext", "buff_top", "oel_burner_on", "pump_hz_on", "hz_vl"]

    def __init__(self, perz_base: dict[str, float], gain: dict[str, float]) -> None:
        self.perz_base = perz_base
        self.offset_base = dict(OFF)
        self.gain = gain

    def predict_raw(self, raw, tsa, targets=None, horizons=None) -> dict:
        out: dict = {}
        for t in targets or list(ROLLOUT_TARGETS):
            for h in horizons or list(self.horizons):
                out[(t, int(h))] = np.array([self._val(t, raw)], dtype=np.float64)
        return out

    def _val(self, t: str, raw: dict[str, np.ndarray]) -> float:
        if t.startswith("ras_") and "_perc" in t:
            z = t.split("_")[1]
            delta = float(np.asarray(raw[f"offset_{z}"], dtype=np.float64)[0]) - self.offset_base[z]
            return self.perz_base[z] - self.gain[z] * delta
        if t in ("oel_burner_on", "pump_hz_on"):
            return float(np.asarray(raw[t], dtype=np.float64)[0])
        if t == "hz_vl":
            return 25.0
        return 0.0


def _store_eq(root: Path) -> Path:
    """Gleichgewichts-Store: alle 3 Zonen perz = 20,0 (Ist = 20 − Off;
    DG: Ist 18,6 @ off +1,4 → Soll 18,6 sitzt exakt drauf)."""
    _set_cols(
        root,
        ras_eg_ist=21.8,  # perz 20,0 @ off −1,8
        ras_dg_ist=18.6,  # perz 20,0 @ off +1,4
        ras_eg_perc=20.0,
        ras_dg_perc=20.0,
        ras_eg_perc_last=20.0,
        ras_dg_perc_last=20.0,
    )
    return root


def _perz_traj() -> dict[int, dict[int, float]]:
    """Gleichgewichts-Perzipiert (perz = 20,0) für alle Zonen/5 Stützpunkte."""
    return {z: {h: 20.0 for h in (0, 1, 4, 6, 12)} for z in config.ZONES}


def test_intent_ableitung_und_h0_identitaet() -> None:
    """Fix-DoD: intent = ras_soll_now(off_now) = 20 − off_now (KEIN Δ) und
    h=0-Identität: Abweichung bei h=0 = |ist_now − intent| für ALLE Δ."""
    # Ableitung: Keller off +3,5 → Intent 16,5 ≠ 20; +1,1 → 18,9; −1,7 → 21,7.
    assert ras_soll_now(3.5) == 16.5
    assert ras_soll_now(1.1) == 18.9
    assert ras_soll_now(-1.7) == 21.7
    assert ras_soll_now(1.4) == 18.6  # live-DG-Beleg (Ist 18,6 @ off +1,4)
    assert config.RAS_BASE_C == 20.0  # EINE Konstante (nichts je-Zone)

    # h=0-Identität (comfort_cost-Unit-Test): Keller zu warm (perz_now 25,2,
    # off +3,5 → ist_now 21,7, intent 16,5). Rollout-Semantik: h=0 =
    # perz_now + Δ (apply_offset_step-Buchführung) → ist_cand[0] = ist_now
    # → h=0-Beitrag 4,7² KLEINER-ALS-alle-Δ-identisch; h>0 (Raum haltend,
    # perz 25,2) unterscheidet korrekt: Δ<0 (Soll rauf = wärmer) teurer.
    off = {z: 0.0 for z in config.ZONES}
    off["kell"] = 3.5
    perz_now = 25.2  # ist_now 21,7, intent 16,5 → h=0-Dev |21,7 − 16,5| = 5,2 K
    in_band = {z: {h: 20.0 for h in (0, 1, 4, 6, 12)} for z in ("eg", "dg")}
    for dk in (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        cand = {"kell": dk, "eg": 0.0, "dg": 0.0}
        # Rollout-Struktur: h=0 = apply_offset_step-Buchführung (perz_now +
        # Δ) → ist_cand[0] = perz_now + Δ − (off_now + Δ) = ist_now (fix);
        # h>0 = Raum haltend (perz_now, RAS-Regel hält, kein L1-Transient).
        traj = {
            "kell": {h: (perz_now + dk if h == 0 else perz_now) for h in (0, 1, 4, 6, 12)},
            **in_band,
        }
        c_dk = comfort_cost(traj, off, cand)
        # Exakte Zerlegung: h=0-Identität (Abweichung 5,2 K → 4,7² für ALLE
        # Δ, Band wird VOR dem Quadrieren abgezogen) + 11 h ×
        # Band-Überschreitung |5,2 − Δ| (h>0, korrekt Δ-abhängig: Δ<0 =
        # wärmer → mehr Abweichung).
        expected = 4.7 ** 2 + 11 * max(0.0, abs(5.2 - dk) - 0.5) ** 2
        assert abs(c_dk - expected) < 1e-9, (dk, c_dk, expected)
    c_minus = comfort_cost(
        {"kell": {h: (perz_now - 0.5 if h == 0 else perz_now) for h in (0, 1, 4, 6, 12)}, **in_band},
        off, {"kell": -0.5, "eg": 0.0, "dg": 0.0})
    c_zero = comfort_cost({"kell": {h: perz_now for h in (0, 1, 4, 6, 12)}, **in_band},
                          off, {"kell": 0.0, "eg": 0.0, "dg": 0.0})
    c_plus = comfort_cost(
        {"kell": {h: (perz_now + 0.5 if h == 0 else perz_now) for h in (0, 1, 4, 6, 12)}, **in_band},
        off, {"kell": 0.5, "eg": 0.0, "dg": 0.0})
    # Zu warm: Δ<0 (wärmer) strikt TEURER, Δ>0 (kühler) günstiger.
    assert c_minus > c_zero > c_plus


def test_dg_gleichgewichts_regression(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-1 (DG-artig, Gleichgewicht): alle Zonen perz = 20,0 (Ist = 20 − Off,
    DG: Ist 18,6 @ off +1,4 → Soll 18,6 sitzt exakt drauf) → injizierter
    Rollout hält perz bei 20 → **keine** Offset-Änderung (kein Kandidat
    schlägt No-Change um ≥ NO_CHANGE_MIN_DELTA)."""
    root = _store_eq(make_mini_store(tmp_path / "v003_eq"))
    p = FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 20.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 0.0},  # RAS regelt perz konstant 20
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    tier_a = [e for e in rec.empfehlungen if e["tier"] == "A"]
    for e in tier_a:
        assert e["changed"] is False, e["parameter"]
        assert e["aktuell"] == e["vorgeschlagen"]
    # No-Change = bester Kandidat (alle Δ teurer, Invarianz: perz 20 →
    # |ist_cand − soll_cand| = |perz − 20| = 0 im Rollout-Inneren).
    assert rec.kosten["beste"]["kandidat"] == "kell:+0.0+eg:+0.0+dg:+0.0"
    assert rec.konfidenz == min(config.CONF_NO_CHANGE, config.CONF_CAP)


def _kell_delta(kandidat: str) -> float:
    """Keller-Komponente eines Kandidaten-Schlüssels (z. B. 'kell:+0.5+…')."""
    import re

    return float(re.search(r"kell:([+-]?\d+\.\d)", kandidat).group(1))


def test_keller_richtungs_regression(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-2 (Trigger-Fall, zu warm): Keller perz 25,2 (Ist 21,7 @ off +3,5 →
    Soll 16,5; Raum ZU WARM) → **keine** Offset-Reduktion (Δ<0) gewinnt;
    bester Kandidat: Keller-Offset ≥ Basis-Offset (kühlen oder No-Change);
    der Δ=−0,5-Kandidat muss teurer sein als No-Change. (v0.2-Fehler: Δ<0
    = 'wärmer' hätte gewonnen.)"""
    root = make_mini_store(tmp_path / "v003_hot")
    _set_cols(root, ras_kell_ist=21.7, ras_kell_perc=25.2, ras_kell_perc_last=25.2)
    p = FakePredictorV3(
        perz_base={"kell": 25.2, "eg": 20.0, "dg": 20.0},
        gain={"kell": 1.0, "eg": 0.0, "dg": 0.0},  # Keller: Offset↑ kühlt perz
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    kell = by_param["RAS-Offset kell"]
    assert kell["aktuell"] == "+3.5 K"
    costs = {row["kandidat"]: row["cost"] for row in rec.candidate_list}
    basis = costs["kell:+0.0+eg:+0.0+dg:+0.0"]
    # (a) Δ=−0,5 (wärmer) ist teurer als No-Change (Richtungs-Regression).
    assert costs["kell:-0.5+eg:+0.0+dg:+0.0"] > basis
    # (b) Kein Keller-Δ<0-Kandidat schlägt No-Change.
    for key, cost in costs.items():
        if _kell_delta(key) < 0.0:
            assert cost >= basis, key
    # (c) Bester Kandidat: Keller ≥ Basis-Offset (kühlen oder No-Change).
    best = rec.kosten["beste"]["kandidat"]
    assert _kell_delta(best) >= 0.0, best
    # (d) Empfehlung: Keller NICHT gesenkt (nur No-Change oder Anheben).
    vorgeschlagen = float(kell["vorgeschlagen"].split()[0].replace("−", "-"))
    assert vorgeschlagen >= 3.5, kell


def test_zu_kalt_richtungs_regression(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """Neu (FIX): zu-kalt-Richtung — DG perz 17,0 haltend (Ist 15,6 @ off +1,4,
    Intent 18,6 → Raum 3,0 K unter Intent). Offset-AB (Δ<0) heizt (perz
    Richtung 20, RAS-Feedback): bester Δ ≤ 0 (Soll runter = mehr Heizung,
    korrekt); Δ>0 (kühlen) verliert strikt."""
    root = _store_eq(make_mini_store(tmp_path / "v003_cold"))
    _set_cols(
        root,
        ras_dg_ist=15.6,  # perz 17,0 @ off +1,4 (Ist = 20,0 − 1,4 − 3,0)
        ras_dg_perc=17.0,
        ras_dg_perc_last=17.0,
    )
    p = FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 17.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 1.0},  # DG: Offset↓ heizt perz
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    dg = by_param["RAS-Offset dg"]
    costs = {row["kandidat"]: row["cost"] for row in rec.candidate_list}
    basis = costs["kell:+0.0+eg:+0.0+dg:+0.0"]

    def _dg_delta(key: str) -> float:
        import re

        return float(re.search(r"dg:([+-]?\d+\.\d)", key).group(1))

    # (a) Kein Δ>0-Kandidat (kühlen = vom Intent weiter weg) schlägt No-Change.
    for key, cost in costs.items():
        if _dg_delta(key) > 0.0:
            assert cost > basis, key
    # (b) Bester Kandidat: DG-Δ ≤ 0 (Soll runter = mehr Heizung).
    best = rec.kosten["beste"]["kandidat"]
    assert _dg_delta(best) <= 0.0, best
    # (c) Empfehlung: DG-Offset NICHT angehoben (nur No-Change oder Absenken).
    vorgeschlagen = float(dg["vorgeschlagen"].split()[0].replace("−", "-"))
    assert vorgeschlagen <= 1.4, dg


def test_anzeige_ist_soll_abw(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-4: Tier-A-Empfehlung trägt „Ist … (Soll …, Abw. … K)" (Ist statt
    Perzipiert; Abweichung vorzeichnungsgetreu, + = zu warm); das alte
    „Perzipiert … (Soll 20 °C, Abw. …)" entfällt.

    DG: Ist 18,6 @ off +1,4 → Soll 18,6, Abw. +0.0 K; Keller: Ist 16,5 @ off
    +3,5 → Soll 16,5, Abw. +0.0 K; EG: Ist 21,8 @ off −1,8 → Soll 21,8, Abw.
    +0,0 K."""
    root = _store_eq(make_mini_store(tmp_path / "v003_txt"))
    p = FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 20.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 0.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    for e in rec.empfehlungen:
        if e["tier"] != "A":
            continue
        b = e["begruendung_de"]
        assert b.startswith("Ist "), e["parameter"]
        assert "(Soll " in b and "Abw. " in b, e["parameter"]
        assert "Perzipiert" not in b  # altes Format entfällt
        assert "None" not in b
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    assert "Ist 18.6 °C (Soll 18.6 °C, Abw. +0.0 K)" in by_param["RAS-Offset dg"]["begruendung_de"]
    assert "Ist 16.5 °C (Soll 16.5 °C, Abw. +0.0 K)" in by_param["RAS-Offset kell"]["begruendung_de"]
    assert "Ist 21.8 °C (Soll 21.8 °C, Abw. +0.0 K)" in by_param["RAS-Offset eg"]["begruendung_de"]


def test_model_version_v003_und_determinismus(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-5: model_version = cmi-brain-v003+l2-v005 (v0.5-Identität, Ticket D;
    JSON + Recommendation) und Determinismus (zwei Läufe → identisches JSON)."""
    root = _store_eq(make_mini_store(tmp_path / "v003_mv"))
    p1 = FakePredictorV3({"kell": 20.0, "eg": 20.0, "dg": 20.0},
                         {"kell": 0.0, "eg": 0.0, "dg": 0.0})
    p2 = FakePredictorV3({"kell": 20.0, "eg": 20.0, "dg": 20.0},
                         {"kell": 0.0, "eg": 0.0, "dg": 0.0})
    r1 = recommend(TS, features_dir=root, wx=wx_ok, predictor=p1, skill_map=skill_map)
    r2 = recommend(TS, features_dir=root, wx=wx_ok, predictor=p2, skill_map=skill_map)
    assert config.L2_VERSION == "l2-v005"  # v0.5: ΔT-Diagnose + Lag (Ticket D)
    assert config.model_version() == "cmi-brain-v003+l2-v005"
    assert r1.model_version == "cmi-brain-v003+l2-v005"
    assert r1.to_json()["model_version"] == "cmi-brain-v003+l2-v005"
    assert r1.to_json() == r2.to_json()


def test_rollout_ist_semantik(tmp_path: Any) -> None:
    """v0.3-FIX-Semantik-Verifikation: Rollout (FakePredictor) + comfort_cost —
    h=0 = ungestörter Ist-Wert (perz − off_now), Komfort misst Ist gegen den
    fixen Intent (20 − off_now, kein Δ) + h=0-Identität."""
    root = make_mini_store(tmp_path / "v003_ro")
    _set_cols(root, ras_kell_ist=21.7, ras_kell_perc=25.2, ras_kell_perc_last=25.2)
    st = load_state(TS, features_dir=root)
    p = FakePredictorV3(
        perz_base={"kell": 25.2, "eg": 20.0, "dg": 20.0},
        gain={"kell": 1.0, "eg": 0.0, "dg": 0.0},
    )

    raw_cols = list(dict.fromkeys(list(p.raw_inputs) + list(ROLLOUT_TARGETS) + list(OFFSET_STEP_COLS)))
    tsa, raw_rows, _ = load_store(root, columns=raw_cols, where=f"ts = TIMESTAMP '{TS}'")
    raw = {k: v[:1] for k, v in raw_rows.items()}
    tsa = tsa[:1]
    # No-Change: perz 25,2 (alle h), ist_cand = perz − off_now = 21,7,
    # Intent (fix) = 20 − 3,5 = 16,5 → Abweichung 5,2 K (vor dem Band 4,7 K)
    # über 12 h — identisch für ALLE Δ bei h=0 (nichts ist passiert).
    ro0 = rollout(p, st.state_ts_str, raw, tsa, {"kell": 0.0, "eg": 0.0, "dg": 0.0})
    assert abs(ro0[0]["ras_kell_perc"] - 25.2) < 1e-9  # h=0 = ungestörter perz-Ist
    c0 = candidate_cost(ro0, {"kell": 0.0, "eg": 0.0, "dg": 0.0}, st.offsets)
    assert abs(c0.comfort - 12 * 4.7 ** 2) < 1e-9
    assert c0.change == 0.0
    # Δ=+0,5 (Offset↑ = kühlen): h=0-Buchführung perz 25,7 →
    # ist_cand[0] = 25,7 − 4,0 = 22,2 (h=0-Identität: h=0-Dev 5,7 K =
    # |ist_now − intent| + Δ); RAS-Feedback: perz(h>0) = 25,2 − 0,5 = 24,7
    # → ist_cand(h>0) = 20,7 vs. Intent 16,5 (FIX: kein Δ) → 4,7 K im
    # Innenhorizont (Offset↑ kühlt, korrekte Richtung).
    ro_up = rollout(p, st.state_ts_str, raw, tsa, {"kell": 0.5, "eg": 0.0, "dg": 0.0})
    assert abs(ro_up[0]["ras_kell_perc"] - 25.7) < 1e-9
    c_up = candidate_cost(ro_up, {"kell": 0.5, "eg": 0.0, "dg": 0.0}, st.offsets)
    # h=0: Abweichung 5,2 K → 4,7² (h=0-Identität); h>0: 20,7 − 16,5 = 4,2 K
    # vor dem Band → 3,7² (Offset↑ kühlt, korrekt).
    assert abs(c_up.comfort - 4.7 ** 2 - 11 * 3.7 ** 2) < 1e-9
    assert c_up.comfort < c0.comfort  # Offset↑ = Raum kühlt (korrekte Richtung)
    assert c_up.change == 0.5 * 0.5  # Änderungs-Strafe für Δ = +0,5 K
