"""Guardrails (hart, vor Ausgabe): Puffer>70, Frost, ±2 K/h, Solar.

Test-States: die Fixtures nutzen denselben Mini-Store; ``State`` wird per
``load_state`` gebaut und gezielt überschrieben (Puffer/Frost-Lage).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analysis.model.l2 import config
from analysis.model.l2.recommend import ALL_GRID, apply_guardrails, tier_b_advisories
from analysis.model.l2.tests.conftest import patch_state

CANDS = [
    {"kell": 0.0, "eg": 0.0, "dg": 0.0},
    {"kell": -0.5, "eg": 0.0, "dg": 0.0},
    {"kell": 0.5, "eg": 0.0, "dg": 0.0},
]


def test_buffer_overfull_no_load_rejects_senken(tmp_path: Path, st: Any, wx_ok: Any) -> None:
    """Puffer > 70 °C + Lade-Anforderung (Pumpe an) → Offset-Senkung verworfen."""
    state = patch_state(st, buff_top=75.0, pump_hz_on=1.0)
    kept, hits = apply_guardrails([dict(c) for c in CANDS], state, wx_ok)
    # Senken (Δ=-0,5) raus, Anheben (Δ=+0,5) und Basis drin.
    assert {"kell": -0.5, "eg": 0.0, "dg": 0.0} not in kept
    assert {"kell": 0.5, "eg": 0.0, "dg": 0.0} in kept
    assert {"kell": 0.0, "eg": 0.0, "dg": 0.0} in kept
    assert any("70" in h and "Lade" in h for h in hits)


def test_buffer_overfull_no_load_no_reject(tmp_path: Path, st: Any, wx_ok: Any) -> None:
    """Puffer > 70 °C ohne Lade-Anforderung → Gate nicht aktiv (nichts zu laden)."""
    state = patch_state(st, buff_top=75.0, pump_hz_on=0.0, oel_burner_on=0.0)
    kept, hits = apply_guardrails([dict(c) for c in CANDS], state, wx_ok)
    assert {"kell": -0.5, "eg": 0.0, "dg": 0.0} in kept
    assert not any("Lade" in h for h in hits)


def test_frost_low_buffer_rejects_senken(tmp_path: Path, st: Any, wx_frost: Any) -> None:
    """Frost + Puffer < 40 °C → Offset nie senken (selbst ohne Lade-Anforderung)."""
    state = patch_state(st, buff_top=35.0, pump_hz_on=0.0)
    kept, hits = apply_guardrails([dict(c) for c in CANDS], state, wx_frost)
    assert {"kell": -0.5, "eg": 0.0, "dg": 0.0} not in kept
    assert {"kell": 0.5, "eg": 0.0, "dg": 0.0} in kept
    assert any("Frost" in h for h in hits)


def test_frost_not_active_without_frost(tmp_path: Path, st: Any, wx_ok: Any) -> None:
    state = patch_state(st, buff_top=35.0, pump_hz_on=0.0)
    kept, _ = apply_guardrails([dict(c) for c in CANDS], state, wx_ok)
    assert {"kell": -0.5, "eg": 0.0, "dg": 0.0} in kept


def test_max_delta_per_hour_grid_guarantee(tmp_path: Path, st: Any, wx_ok: Any) -> None:
    """Alle Grid-Kandidaten ≤ ±2 K/h (Grid-Guarantie, explizit geprüft)."""
    kept, hits = apply_guardrails([dict(c) for c in ALL_GRID[:50]], st, wx_ok)
    for c in kept:
        for z in config.ZONES:
            assert abs(c[z]) <= 2.0 + 1e-9
    assert not any("> ±2" in h for h in hits)


def test_solar_guardrail_in_tier_b_not_candidate(tmp_path: Path, st: Any, wx_ok: Any) -> None:
    """Kollektor < 25 °C = Tier-B-Advisory (keine Solar-Hand-EIN), keine Kandidaten-Aussortierung."""
    state = patch_state(st, solar_coll=20.0)
    hits: list[str] = []
    adv = tier_b_advisories(state, wx_ok, hits)
    assert any("solar" in h for h in hits)
    # Heizkurve-Advisory trotzdem vorhanden (regelnbasiert, kein Rollout).
    assert any(a["parameter"].startswith("Heizkurve") for a in adv)
