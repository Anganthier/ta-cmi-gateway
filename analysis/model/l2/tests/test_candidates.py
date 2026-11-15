"""Kandidaten: Grid, Pruning, Cap, Quantisierung (deterministisch)."""
from __future__ import annotations

from analysis.model.l2 import config
from analysis.model.l2.recommend import ALL_GRID, generate_candidates, quantize_delta


def test_all_grid_343() -> None:
    """Grid {0,±0,5,±1,±2}³ = 7³ = 343 (Cap-Limit ±2 K/h erfüllt)."""
    assert len(ALL_GRID) == 343
    for c in ALL_GRID:
        for z in config.ZONES:
            assert c[z] in config.OFFSET_CANDIDATES


def test_pruning_rule() -> None:
    """Pruning: ≥2 Zonen nicht-0 → |Δ|≤1 K; einzelne Zone → |Δ|≤2 K."""
    offs = {"kell": 0.0, "eg": 0.0, "dg": 0.0}
    all_pruned = generate_candidates(offs, max_candidates=10_000)
    for cand in all_pruned:
        nonzero = sum(1 for d in cand.values() if d != 0.0)
        limit = config.PRUNE_MULTI_MAX_K if nonzero >= 2 else config.PRUNE_SINGLE_MAX_K
        assert all(abs(d) <= limit for d in cand.values()), cand
    # Einzelne Zone darf ±2 K (z. B. eg:+2.0), zwei Zonen nicht (eg:+2.0+dg:+0.5).
    assert {"kell": 0.0, "eg": 2.0, "dg": 0.0} in all_pruned
    assert {"kell": 0.0, "eg": 2.0, "dg": 0.5} not in all_pruned
    assert {"kell": 1.0, "eg": 1.0, "dg": 0.0} in all_pruned


def test_cap_includes_base() -> None:
    """Cap (Default 30) in Distanz-Reihenfolge → Basisaktion (0,0,0) bleibt drin."""
    cands = generate_candidates({"kell": 1.0, "eg": -1.0, "dg": 0.5})
    assert len(cands) == config.MAX_CANDIDATES
    assert cands[0] == {"kell": 0.0, "eg": 0.0, "dg": 0.0}


def test_deterministic_order() -> None:
    a = generate_candidates({"kell": 0.0, "eg": 0.0, "dg": 0.0})
    b = generate_candidates({"kell": 0.0, "eg": 0.0, "dg": 0.0})
    assert a == b


def test_quantize_delta() -> None:
    """0,5-K-Schritte: 0.5-K-Quantisierung (Guardrail §3.2 R4)."""
    assert quantize_delta(0.0) == 0.0
    assert quantize_delta(0.5) == 0.5
    assert quantize_delta(1.0) == 1.0
    assert quantize_delta(-0.5) == -0.5
    # 0,1-K-Reste aus dem Store (Δ bleibt 0,5-K-gesteppt, wenn Δ ganzzahlig).
    assert quantize_delta(0.0) == 0.0
    # Runden (0,25 → 0,0; 0,75 → 1,0): nur Δ aus dem Grid kommen, aber robust.
    assert quantize_delta(0.25) == 0.0
    assert quantize_delta(0.75) == 1.0
    assert quantize_delta(1.25) == 1.0
