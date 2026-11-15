"""M2-Test-Setup: Repo-Root auf sys.path + deterministischer Mini-Feature-Store.

Keine echten Trainings-Läufe mit vollem Feature-Store; alle Tests nutzen
(1) einen synthetischen Mini-Store (Hive-Layout, 198 Rows, 3 Splits über
Jahresgrenzen) und (2) Mini-Modelle (≤ 2 Epochs, kleine Stämme). TF/LGBM/CPU
deterministisch (Seed 20261031).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from analysis.model.m2 import config  # noqa: E402

SEED = config.SEED
N_FEATURES = config.N_FEATURES
N_TARGETS = config.N_TARGETS
N_FLAGS = N_FEATURES + len(config.ANALOG_TARGETS)  # extended dimension


def _mini_ts_blocks() -> list[datetime]:
    """Drei lückenlose Stunden-Blöcke über Jahres-/DST-Grenzen:
    A=Train (2024-12, 60 h), B=Valid (2025-01, 90 h), C=Test (2026-01, 48 h).
    Zwischen den Blöcken: Zeit-Sprünge (LEAD überspringt → Tests der
    Drop-/Split-Logik, nicht der Lag-Kontinuität)."""
    a = [datetime(2024, 12, 20) + timedelta(hours=i) for i in range(60)]
    b = [datetime(2025, 1, 10) + timedelta(hours=i) for i in range(90)]
    c = [datetime(2026, 1, 5) + timedelta(hours=i) for i in range(48)]
    return a + b + c


def build_mini_store(tmp_path: Path, seed: int = SEED) -> Path:
    """Schreibt einen deterministischen Mini-Feature-Store (Hive YYYY/MM/,
    198 Rows: 60 Train + 90 Valid + 48 Test).

    Columns: ``ts``, ``gap_full`` + alle ``FEATURE_INPUTS`` + alle
    ``TARGETS`` (die einzigen, die ``transitions._query_transitions``
    referenziert). Deterministische Zufalls-Fills; ``solar_coll`` (State +
    Target) NULL im Train-Block (Imputations- + Flag-Test), 3 Rows mit
    ``gap_full=1`` (Drop-Test: rows 10, 11 = Train, row 65 = Valid).
    Returns Store-Wurzel.
    """
    rng = np.random.default_rng(seed)
    ts = _mini_ts_blocks()
    n = len(ts)
    # Split-Beginn-Indizes (für gap_full-Set):
    a_end = 60
    gap_full = np.zeros(n, dtype=np.int64)
    gap_full[10] = 1  # Train-Row → Input-Drop (gap_current)
    gap_full[11] = 1  # Train-Row → Input-Drop (gap_current); Row 9 → gap_next-Drop
    gap_full[65] = 1  # Valid-Row → Input-Drop; Valid-Row 64 → gap_next-Drop

    def _col(shape, lo=-1.0, hi=1.0):
        return (lo + (hi - lo) * rng.random(shape)).astype("float32")

    cols: dict[str, np.ndarray] = {}
    # FEATURE_INPUTS
    for c in config.FEATURE_INPUTS:
        cols[c] = _col((n,))
    # Solar-Kanal NULL im Train-Block (State-Input)
    sc_state = np.where(np.arange(n) < a_end, np.nan, cols["solar_coll"])
    cols["solar_coll"] = sc_state
    # TARGETS
    for t in config.TARGETS:
        cols[t] = _col((n,), lo=0.0, hi=2.0)
    # Digital-Targets ∈ [0,1]
    for t in config.DIGITAL_TARGETS:
        cols[t] = rng.random((n,)).astype("float32")
    # Solar-Target NULL im Train-Block
    cols["solar_coll"] = np.where(np.arange(n) < a_end, np.nan, cols["solar_coll"])

    # ts als naive Berlin-Timestamps
    ts_arr = np.array([t.strftime("%Y-%m-%d %H:%M:%S") for t in ts], dtype="datetime64[s]")

    names = ["ts", "gap_full"] + list(config.FEATURE_INPUTS) + list(config.TARGETS)
    arrays = [ts_arr, gap_full] + [cols[c] for c in config.FEATURE_INPUTS] + [
        cols[t] for t in config.TARGETS
    ]
    table = pa.table({nm: a for nm, a in zip(names, arrays, strict=True)})

    # Dateien pro (year, month)
    by_ym: dict[tuple[int, int], list[int]] = {}
    for i, t in enumerate(ts):
        by_ym.setdefault((t.year, t.month), []).append(i)
    for (y, m), idxs in by_ym.items():
        d = tmp_path / f"{y}" / f"{m:02d}"
        d.mkdir(parents=True, exist_ok=True)
        sub = table.take(pa.array(idxs))
        pq.write_table(sub, d / f"{y}-{m:02d}.parquet")
    return tmp_path


def mini_splits(store_root: Path | None = None, tmp_path: Path | None = None, **kw):
    """Baut Transitions vom (ggf. neu erstellten) Mini-Store. Returns
    (splits, stats, X_ext, scaler)."""
    from analysis.model.m2 import normalize, transitions

    if store_root is None:
        store_root = build_mini_store(tmp_path)
    splits, stats = transitions.build_transitions(features_dir=store_root, split_test=True)
    X_ext: dict[str, np.ndarray] = {}
    flags: list[str] = []
    for nm in ("train", "valid", "test"):
        if nm in splits:
            X, fl = transitions.with_null_flags(splits[nm])
            X_ext[nm] = X
            flags = fl
    scaler = normalize.StandardScaler.fit(X_ext["train"])
    return splits, stats, X_ext, flags, scaler


def make_mini_keras(n_features: int, seed: int = SEED):
    """Kleines Keras-Modell (Stamm 16, Head 4) für Tests."""
    from analysis.model.m2 import model

    return model.build_model(
        n_features,
        targets=config.TARGETS,
        hidden=(16,),
        dropout=0.0,
        head_hidden=4,
        learning_rate=1e-2,
    )


def train_mini_keras(model, X_train, Y_train, X_valid, Y_valid, epochs: int = 2):
    y_train = {f"head_{t}": Y_train[:, i] for i, t in enumerate(config.TARGETS)}
    y_valid = {f"head_{t}": Y_valid[:, i] for i, t in enumerate(config.TARGETS)}
    return model.fit(
        X_train,
        y_train,
        validation_data=(X_valid, y_valid),
        epochs=epochs,
        batch_size=32,
        verbose=0,
    )


def make_mini_lgbm(X_train, Y_train, X_valid, Y_valid, boost: int = 6):
    """Kleines deterministisches LGBM-Ensemble für Tests."""
    from analysis.model.m2 import lgbm_model

    params = lgbm_model.lgbm_params()
    params.update({"num_leaves": 4, "min_child_samples": 2, "learning_rate": 0.3})
    ens, _ = lgbm_model.train_ensemble(
        X_train,
        Y_train,
        X_valid,
        Y_valid,
        num_boost_round=boost,
        early_stopping_rounds=2,
        weights={t: 1.0 for t in config.TARGETS},
    )
    return ens


@pytest.fixture(autouse=True)
def _no_gpu_reexec(monkeypatch):
    """Schützt ALLE Tests vor dem GPU-Self-Re-Exec in ``build.main()``:
    ``CMI_M2_GPU_REEXEC=1`` → der Re-Exec-Pfad wird übersprungen (Host-Pfad
    ist der CLI-Start ohne Shell-Activate; Tests rufen ``build()``/``main()``
    im Prozess). Re-Exec-Tests löschen das Env selbst (monkeypatch)."""
    monkeypatch.setenv("CMI_M2_GPU_REEXEC", "1")


@pytest.fixture(scope="session")
def mini_store(tmp_path_factory):
    """Deterministischer Mini-Store → (splits, stats, X_ext, flags, scaler).

    Session-gecacht (Store + Transitions + Scaler sind deterministisch).
    """
    root = tmp_path_factory.mktemp("minifeatures")
    return mini_splits(build_mini_store(root))


@pytest.fixture
def mini_keras(mini_store):
    """Frisches, untrainiertes Mini-Keras-Modell (Session-Store)."""
    splits, stats, X_ext, flags, scaler = mini_store
    return make_mini_keras(X_ext["train"].shape[1])
