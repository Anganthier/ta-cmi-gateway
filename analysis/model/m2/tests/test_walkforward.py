"""Tests: Walk-forward-Splits (keine Zeitlein-Überschneidung:
max(train ts) < min(valid ts) < min(test ts)) + Split-Zuordnung."""
from __future__ import annotations

from datetime import datetime

import pytest

from analysis.model.m2 import transitions


def test_no_timeline_overlap(mini_store):
    splits, stats, *_ = mini_store
    train, valid, test = splits["train"], splits["valid"], splits["test"]
    assert max(train.ts) < min(valid.ts)
    assert max(valid.ts) < min(test.ts)
    # Strikter Jahres-Constraint.
    assert max(train.ts) < datetime(2025, 1, 1)
    assert min(valid.ts) >= datetime(2025, 1, 1)
    assert max(valid.ts) < datetime(2026, 1, 1)
    assert min(test.ts) >= datetime(2026, 1, 1)


def test_assert_no_leakage_passes(mini_store):
    splits, *_ = mini_store
    # Kein AssertionError → Test bestanden.
    transitions.assert_no_leakage(*splits.values())


def test_assert_no_leakage_fails_on_overlap(tmp_path):
    """Synthetische Splits mit Überschneidung → AssertionError."""
    from analysis.model.m2.transitions import Split

    def _mk(name, ts):
        n = len(ts)
        return Split(
            name=name,
            X=__import__("numpy").zeros((n, 3), dtype="float32"),
            Y=__import__("numpy").zeros((n, 2), dtype="float32"),
            ts=ts,
            feature_null_mask=__import__("numpy").zeros((n, 3), dtype="uint8"),
            target_null_mask=__import__("numpy").zeros((n, 2), dtype="uint8"),
        )

    train = _mk("train", [datetime(2025, 1, 1)])
    valid = _mk("valid", [datetime(2024, 12, 31)])  # < train → Leak
    with pytest.raises(AssertionError):
        transitions.assert_no_leakage(train, valid)


def test_split_counts_sum_to_transitions(mini_store):
    splits, stats, *_ = mini_store
    total = sum(len(s.ts) for s in splits.values())
    assert total == stats.n_transitions
    assert stats.n_transitions == stats.n_rows_total - (
        stats.n_drops_gap_current
        + stats.n_drops_missing_next
        + stats.n_drops_gap_full_next
    )
