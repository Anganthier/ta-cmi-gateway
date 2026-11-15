"""Tests: Übergangs-Bau (Monats-/DST-Grenzen, gap_full-Drop, fehlende Next-Row,
NULL-Imputation + Flags, Walk-forward-Kein-Leakage)."""
from __future__ import annotations

import numpy as np

from analysis.model.m2 import config, transitions


def test_split_sizes_and_drops(mini_store):
    splits, stats, X_ext, flags, scaler = mini_store
    # Drops: gap_current = rows 10,11,65 (3); gap_next = rows 9,64 (2);
    # missing_next = letzte Row (1) → 6 Drops → 198 - 6 = 192 Transitions.
    assert stats.n_rows_total == 198
    assert stats.n_drops_gap_current == 3
    assert stats.n_drops_gap_full_next == 2
    assert stats.n_drops_missing_next == 1
    assert stats.n_transitions == 192
    # Split-Größen: Train 57 (60-3), Valid 88 (90-2), Test 47 (48-1).
    assert stats.split_sizes["train"] == 57
    assert stats.split_sizes["valid"] == 88
    assert stats.split_sizes["test"] == 47
    for nm, s in splits.items():
        assert len(s.ts) == stats.split_sizes[nm]
        assert s.X.shape[0] == len(s.ts)
        assert s.Y.shape[0] == len(s.ts)
        assert s.X.shape[1] == config.N_FEATURES
        assert s.Y.shape[1] == config.N_TARGETS
    # Flags: extended Dimension = Features + Input-Flags + Analog-Target-Flags.
    expected = config.N_FEATURES + len(config.FEATURE_INPUTS) + len(config.ANALOG_TARGETS)
    for X in X_ext.values():
        assert X.shape[1] == expected


def test_walk_forward_no_leakage(mini_store):
    splits, *_ = mini_store
    transitions.assert_no_leakage(*splits.values())
    train, valid, test = splits["train"], splits["valid"], splits["test"]
    assert max(train.ts) < min(valid.ts)
    assert max(valid.ts) < min(test.ts)
    # Kalender-Grenzen: kein Train-Row ab 2025, kein Valid-Row ab 2026.
    assert all(t.year < 2025 for t in train.ts)
    assert all(t.year == 2025 for t in valid.ts)
    assert all(t.year >= 2026 for t in test.ts)


def test_ts_strictly_increasing_and_no_dupes(mini_store):
    """Monat-/DST-Grenzen: ts ist in jedem Split strikt steigend, keine
    Duplikate (LEAD koppelt immer die sofortige nächste Kalenderstunde;
    Split-Zuordnung nur über Jahr/Zeitpunkt → DST-kann-keine-Zeile-verlieren)."""
    splits, *_ = mini_store
    for s in splits.values():
        ts = [t for t in s.ts]
        assert len(set(ts)) == len(ts), "keine Duplikate erlaubt"
        for a, b in zip(ts, ts[1:], strict=False):
            assert a < b, f"{a} !< {b}"


def test_null_imputation_and_flags(mini_store):
    """solar_coll NULL im Train-Block → Imputation (keine NaN) + Flags=1."""
    splits, stats, X_ext, flags, scaler = mini_store
    train = splits["train"]
    # Nach Imputation: keine NaN in X / Y.
    assert not np.isnan(train.X).any()
    assert not np.isnan(train.Y).any()
    # Feature-Flag: solar_coll-Input-Flag in train rows = 1 (war NULL).
    sc_idx = config.FEATURE_INPUTS.index("solar_coll")
    assert int(train.feature_null_mask[:, sc_idx].sum()) > 0
    assert int(train.feature_null_mask[:, sc_idx].max()) == 1
    # Target-Flag: solar_coll-Target-Flag in train rows = 1.
    sc_t_idx = config.ANALOG_TARGETS.index("solar_coll")
    assert int(train.target_null_mask[:, sc_t_idx].sum()) > 0
    # Imputations-Count > 0 (solar_coll-NULL-Zellen).
    assert stats.n_imputed_cells > 0
    # Flag-Namen sind stabil + korrekt benannt.
    assert "solar_coll_null" in flags
    assert "solar_coll_tgt_null" in flags
    # Stable Dimension über alle Splits (gleiche Spaltenzahl).
    dims = {X.shape[1] for X in X_ext.values()}
    assert len(dims) == 1


def test_dedupe_via_lead_not_pre_filter(mini_store):
    """gap_full-Row 11 (Train) wird gedropt, OHNE dass Row 10 an Row 12
    gekoppelt wird: Row 10 (gap_next=Row 11=1) MUSS ebenfalls gedropt sein.
    Sonst wäre eine Transition (10 → 12) entstanden (falsch)."""
    splits, stats, *_ = mini_store
    # Row 10 → gap_next = Row 11 (gap_full=1) → Drop (gap_next).
    # Row 11 → gap_current → Drop.
    # Beide fehlen → keine Transition mit Input-Row 10.
    train = splits["train"]
    # Die Train-Split-Ts enthalten weder 2024-12-20T10:00 (Row 10) noch 11:00
    # (Row 11) noch 09:00 (Row 9, drop via gap_next).
    from datetime import datetime

    ts_set = set(train.ts)
    assert datetime(2024, 12, 20, 10) not in ts_set
    assert datetime(2024, 12, 20, 11) not in ts_set
    assert datetime(2024, 12, 20, 9) not in ts_set


def test_missing_next_row_dropped(mini_store):
    """Letzte Row des Rasters (Test-Ende) → keine Next-Row → Drop."""
    splits, stats, *_ = mini_store
    assert stats.n_drops_missing_next == 1
    test = splits["test"]
    from datetime import datetime

    # Letzter Test-Bucket 2026-01-06T23:00 (Row 197) fehlt.
    assert datetime(2026, 1, 6, 23) not in set(test.ts)
