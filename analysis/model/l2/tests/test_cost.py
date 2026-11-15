"""Kostenfunktion: Monotonie, No-change-Semantik, Interpolation."""
from __future__ import annotations

import numpy as np

from analysis.model.l2 import config
from analysis.model.l2.recommend import (
    CostBreakdown,
    candidate_cost,
    change_cost,
    comfort_cost,
    energy_cost,
)

OFF0 = {z: 0.0 for z in config.ZONES}
CAND0 = {z: 0.0 for z in config.ZONES}


def _traj(v: float) -> dict[int, dict[int, float]]:
    """Perzipiert-Trajektorie (Rollout-Struktur) konstant über h, alle Zonen."""
    return {z: {h: v for h in (0, 1, 4, 6, 12)} for z in config.ZONES}


def test_comfort_zero_in_band() -> None:
    """Ist im ±0,5-K-Band um den Intent (perz 20,2, Offsets 0 → Intent 20)
    → Komfort = 0."""
    assert comfort_cost(_traj(20.2), OFF0, CAND0) == 0.0


def test_comfort_monotone_in_deviation() -> None:
    """Größere Abweichung (Ist-Raum vs. Intent) → höhere Kosten (monoton)."""
    base = comfort_cost(_traj(20.6), OFF0, CAND0)   # Ist 20,6 → +0,6 K
    worse = comfort_cost(_traj(21.5), OFF0, CAND0)  # Ist 21,5 → +1,5 K
    assert worse > base > 0.0


def test_comfort_h0_identity_across_deltas() -> None:
    """h=0-Identität (v0.3-FIX): Rollout-Semantik — perz_cand[0] = perz_now +
    Δ (apply_offset_step-Buchführung) → ist_cand[0] = ist_now → der h=0-
    Beitrag = (|ist_now − intent| − Band)² ist für ALLE Δ identisch; h>0
    (Raum haltend, perz konstant) unterscheidet korrekt: zu warm → Δ<0
    (Soll rauf = wärmer) teurer."""
    off = {z: 0.0 for z in config.ZONES}
    off["kell"] = 1.4  # Intent 18,6; perz_now 21,0 → ist_now 19,6 (+1,0 K)
    perz_now = 21.0
    flat = {z: {h: 20.0 for h in (0, 1, 4, 6, 12)} for z in ("eg", "dg")}
    dev0 = abs(perz_now - 20.0)  # = |ist_now − intent| (offset-unsensitive)

    def _term(dev: float) -> float:
        return (dev - 0.5) ** 2 if dev > 0.5 else 0.0

    for dk in (-1.0, -0.5, 0.0, 0.5, 1.0):
        traj = {"kell": {h: (perz_now + dk if h == 0 else perz_now)
                         for h in (0, 1, 4, 6, 12)}, **flat}
        cand = dict(CAND0)
        cand["kell"] = dk
        expected = _term(dev0) + 11 * _term(abs(perz_now - 20.0 - dk))
        assert abs(comfort_cost(traj, off, cand) - expected) < 1e-9, dk
    # Richtungs-Check (zu warm): Δ<0 strikt teurer als No-Change < Δ>0.
    def _traj(dk: float) -> dict[int, dict[int, float]]:
        return {"kell": {h: (perz_now + dk if h == 0 else perz_now)
                         for h in (0, 1, 4, 6, 12)}, **flat}

    cand_dn, cand_up = dict(CAND0), dict(CAND0)
    cand_dn["kell"], cand_up["kell"] = -0.5, 0.5
    assert (comfort_cost(_traj(-0.5), off, cand_dn) > comfort_cost(_traj(0.0), off, CAND0)
            > comfort_cost(_traj(0.5), off, cand_up))


def test_comfort_band_edge() -> None:
    """Genau Band-Überschreitung 0,1 K → Kosten > 0, aber klein (quadratisch)."""
    c = comfort_cost(_traj(20.6), OFF0, CAND0)
    # 0,1² pro Zone/Stunde → 3 Zonen × 12 h × 0,01 = 0,36
    assert abs(c - 0.36) < 1e-9


def test_energy_monotone_in_burner() -> None:
    """Mehr Ölbrenner-An-Quotient → höhere Energie-Kosten (monoton)."""
    low = {h: {"oel_burner_on": 0.1, "hz_vl": 30.0, "pump_hz_on": 0.5} for h in (1, 4, 6, 12)}
    high = {h: {"oel_burner_on": 0.8, "hz_vl": 30.0, "pump_hz_on": 0.5} for h in (1, 4, 6, 12)}
    assert energy_cost(high) > energy_cost(low)


def test_energy_vl_ref() -> None:
    """VL unter Referenz (20 °C) → VL-Term = 0 (nur Öl-Term zählt)."""
    ro = {h: {"oel_burner_on": 0.0, "hz_vl": 15.0, "pump_hz_on": 1.0} for h in (1, 4, 6, 12)}
    assert energy_cost(ro) == 0.0
    ro2 = {h: {"oel_burner_on": 0.0, "hz_vl": 40.0, "pump_hz_on": 1.0} for h in (1, 4, 6, 12)}
    assert energy_cost(ro2) > 0.0


def test_change_cost_formula() -> None:
    """Change = Σ 1[Δ≠0] · 0,5·|ΔK| (je Zone)."""
    assert change_cost({"kell": 0.0, "eg": 0.0, "dg": 0.0}) == 0.0
    assert change_cost({"kell": 1.0, "eg": 0.0, "dg": 0.0}) == 0.5
    assert change_cost({"kell": 1.0, "eg": 1.0, "dg": 0.0}) == 1.0
    assert change_cost({"kell": -0.5, "eg": 0.0, "dg": 0.5}) == 0.5


def test_total_weighting() -> None:
    """Total = w_komfort·Komfort + w_energie·Energie + w_change·Change."""
    cb = CostBreakdown(comfort=1.0, energy=2.0, change=3.0)
    expected = config.W_COMFORT * 1.0 + config.W_ENERGY * 2.0 + config.W_CHANGE * 3.0
    assert abs(cb.total - expected) < 1e-12
    assert cb.as_dict() == {"comfort": 1.0, "energy": 2.0, "change": 3.0, "total": round(expected, 4)}


def test_candidate_cost_no_change_wins_on_flat_state() -> None:
    """Flacher Zustand (Ist im Band um 20 °C, Offsets 0): Basisaktion schlägt
    jede Änderung (Invarianz: Komfort kandidatenunabhängig, nur Change zählt)."""
    base_ro = {h: {z + "_perc": 20.1 for z in config.ZONES} | {"oel_burner_on": 0.0, "pump_hz_on": 0.0, "hz_vl": 20.0}
               for h in (0, 1, 4, 6, 12)}
    base = candidate_cost(base_ro, dict(CAND0), dict(OFF0))
    chg = candidate_cost(base_ro, {"kell": -0.5, "eg": 0.5, "dg": 0.0}, dict(OFF0))
    assert base.total < chg.total
    assert base.total == 0.0
    assert chg.total == config.W_CHANGE * chg.change


def test_candidate_cost_change_wins_when_band_broken() -> None:
    """Keller 5 K über Intent (Ist 25, perz 25, Offset 0 → Intent 20):
    Basis-Komfort hoch; Keller-Offset RAUF (kühlen) senkt die Ist-Abweichung.

    v0.3-FIX-Semantik: ist_cand = perz − cand_offset (25 → 23 → 21), Intent
    fix = 20 (kein Δ) → Abweichung 5 K → 1 K (Band 0,5: Keller-Komfort 0);
    die Änderung bleibt trotzdem die günstigste Gesamtkosten-Option.
    """

    def _ro(kell: float) -> dict[int, dict[str, float]]:
        # Rollout-Struktur {h: {target: Wert}}: perzipiert konstant über h
        # (Offset-Aktion über den Horizont gehalten), alle 5 Stützpunkte.
        return {h: {"ras_kell_perc": kell, "ras_eg_perc": 20.1, "ras_dg_perc": 20.1,
                    "oel_burner_on": 0.0, "pump_hz_on": 0.0, "hz_vl": 20.0}
                for h in (0, 1, 4, 6, 12)}

    off = dict(OFF0)
    base = candidate_cost(_ro(25.0), {"kell": 0.0, "eg": 0.0, "dg": 0.0}, off)
    # Keller-Δ = +2 K (RAUF = kühlen): L1-Transient kühlt perzipiert
    # 25 → 23; abgeleitet: ist_cand = 23 − 2 = 21 vs. Intent 20 → 1 K
    # (im Band) über alle 12 Stunden.
    high = candidate_cost(_ro(23.0), {"kell": 2.0, "eg": 0.0, "dg": 0.0}, off)
    assert base.comfort > high.comfort >= 0.0
    # Gesamtkosten: Komfort-Vorteil (243 → 0) übersteigt die Änderungs-Strafe
    # (0,3 · 0,5 · 2 K = 0,3) klar → die Änderung ist tatsächlich günstiger.
    assert base.total > high.total


def test_interp_points() -> None:
    """_interp_traj: Stützpunkte {0,1,4,6,12} → 13 Stunden linear dazwischen."""
    from analysis.model.l2.recommend import _interp_traj

    vals = {0: 0.0, 1: 1.0, 4: 4.0, 6: 6.0, 12: 12.0}
    full = _interp_traj(vals)
    assert full[0] == 0.0 and full[12] == 12.0
    assert abs(full[2] - (1.0 + (4.0 - 1.0) * (2 - 1) / 3.0)) < 1e-9
    assert abs(full[5] - (4.0 + (6.0 - 4.0) * (5 - 4) / 2.0)) < 1e-9
    assert abs(full[9] - (6.0 + (12.0 - 6.0) * (9 - 6) / 6.0)) < 1e-9
    assert bool(np.isfinite(np.array(list(full.values()))).all())
