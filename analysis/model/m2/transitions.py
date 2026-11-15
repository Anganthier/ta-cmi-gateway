"""M2 — Übergangs-Tabelle: (S_t, A_t, W_t, Cal_t) → Targets @ t+1.

Baut aus dem M1-Feature-Store (``data/features/*/*/*.parquet``, 1 Zeile pro
Kalenderstunde, lückenlos, sortiert) Trainings-Transitions: State/Action/
Wetter/Kalender der Stunde ``t`` und Targets der **nächsten** Zeile
(1-h-ahead).

Regeln (verbindlich):
- Nur wenn beide Rows existieren und ``gap_full`` bei **beiden** = 0
  (sonst Drop + Zählen, s. :class:`TransitionStats`). Das M1-Raster ist
  lückenlos (keine weggelassenen Zeilen; DST-Fallback-Tage zählen 24 Buckets,
  s. M1-SCHEMA §3), daher ist die nächste Row der Index-Nachfolger
  (``LEAD(...) OVER (ORDER BY ts)``).
- NULLs (z. B. ``solar_coll``-Target vor 2022-03-24, Lags vor Raster-Start,
  einzelne ``wx_*``-Felder) → Forward-/Backward-Fill entlang der Zeitachse,
  Rest → Spalten-Median (dokumentiert). Zusätzlich werden **NULL-Flag-
  Spalten** übernommen (``_null`` / ``_tgt_null``), damit das Modell
  "Kanal ausfallen/inaktiv" von "Wert = 0" unterscheiden kann.
- Splits (kalendertreu, walk-forward): Train ``ts < 2025-01-01`` ·
  Valid ``2025`` · Test ``ts >= 2026-01-01`` → ``max(train ts) < min(valid ts)
  < min(test ts)``. Die Ziel-Row einer Transition liegt zeitlich **nach**
  ihrer Input-Row (kein Look-Ahead in den Input-Spalten).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np

from . import config


@dataclass
class TransitionStats:
    """Zählungen für den Report (Drops, Imputations, Split-Größen)."""

    n_rows_total: int = 0
    n_rows_gap_full: int = 0
    n_drops_gap_current: int = 0  # Input-Row selbst gap_full=1 (State unzuverlässig)
    n_drops_missing_next: int = 0  # keine nächste Row (Raster-Ende)
    n_drops_gap_full_next: int = 0  # nächste Row (t+1) hat gap_full=1
    n_transitions: int = 0
    n_imputed_cells: int = 0
    split_sizes: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.split_sizes is None:
            self.split_sizes = {}


@dataclass
class Split:
    """Ein Split (Train/Valid/Test) als NumPy-Arrays.

    ``X`` = (n, n_features) float32 Rohwerte (``config.FEATURE_INPUTS``-
    Reihenfolge; NaNs bereits imputiert).
    ``Y`` = (n, n_targets) float32 Rohwerte (``config.TARGETS``-Reihenfolge).
    ``ts`` = Python-datetime-Liste (naive Berlin-Kalenderstunden).
    ``feature_null_mask`` = (n, n_features) 0/1: Spalte war in der **Input-Row**
    NULL (imputiert; z. B. solar_coll vor 2022-03, Lags am Raster-Start,
    vereinzelt wx_*).
    ``target_null_mask`` = (n, n_targets) 0/1: Spalte war in der **Ziel-Row**
    (t+1) NULL (imputiert; z. B. solar_coll-Target vor 2022-03-24).
    """

    name: str
    X: np.ndarray
    Y: np.ndarray
    ts: list[datetime]
    feature_null_mask: np.ndarray
    target_null_mask: np.ndarray


def _split_of_ts(ts: datetime) -> str:
    """Kalendertreuer Split: Train < 2025 · Valid = 2025 · Test >= 2026."""
    if ts < datetime.fromisoformat(config.VALID_START):
        return "train"
    if ts < datetime.fromisoformat(config.VALID_END):
        return "valid"
    return "test"


def _query_transitions(con: duckdb.DuckDBPyConnection, features_dir: Path) -> list[list[object]]:
    """Query: ``[ts, gap_full, *FEATURE_INPUTS, *LEAD(TARGETS), gap_next,
    next_ts]``.

    Alle Rows werden behalten (kein Vor-Filter auf ``gap_full``): ``LEAD``
    muss über das **komplette** lückenlose Stunden-Raster laufen, damit die
    „nächste Zeile" immer die **sofortige** nächste Kalenderstunde ist
    (``t+1``). Der Drop (``gap_full`` bei aktueller ODER nächster Row = 1,
    bzw. fehlende Next-Row) erfolgt danach in Python — nicht in der Query,
    weil ein Vor-Filter ``LEAD`` über Gap-Stunden hinwegkoppeln ließe und
    falsch an t+2 koppeln würde (statt zu Drop-Per).
    """
    feat = ", ".join(config.FEATURE_INPUTS)
    lead_exprs = ", ".join(
        f"LEAD({t}, 1) OVER w AS t{i}" for i, t in enumerate(config.TARGETS)
    )
    q = f"""
    WITH base AS (
      SELECT ts, gap_full, {feat}, {", ".join(config.TARGETS)}
      FROM '{features_dir}/*/*/*.parquet'
    )
    SELECT
      ts,
      gap_full,
      {feat},
      {lead_exprs},
      LEAD(gap_full, 1) OVER w AS gap_next,
      LEAD(ts, 1) OVER w AS next_ts
    FROM base
    WINDOW w AS (ORDER BY ts)
    """
    return con.execute(q).fetchall()


def build_transitions(
    features_dir: Path | None = None,
    split_test: bool = True,
) -> tuple[dict[str, Split], TransitionStats]:
    """Baut die Übergangs-Tabellen (Train/Valid/Test).

    Parameters
    ----------
    features_dir:
        Feature-Store-Wurzel (Default ``config.FEATURES_DIR``). Tests können
        einen minimalen Test-Store (Hive-Layout, wenige Rows) übergeben.
    split_test:
        Wenn False, wird kein Test-Split gebaut (Mini-Store-Tests).

    Returns
    -------
    ``(splits, stats)``: ``splits`` = ``{"train": Split, "valid": Split[, "test"]}``.
    """
    feats = Path(features_dir) if features_dir is not None else config.FEATURES_DIR
    if not feats.exists():
        raise FileNotFoundError(f"Feature-Store nicht gefunden: {feats}")

    n_feat = len(config.FEATURE_INPUTS)
    n_tgt = len(config.TARGETS)

    con = duckdb.connect()
    try:
        raw_rows = _query_transitions(con, feats)
        n_rows_total = con.execute(
            f"SELECT count(*) FROM '{feats}/*/*/*.parquet'"
        ).fetchone()[0]
        n_rows_gap_full = con.execute(
            f"SELECT count(*) FROM '{feats}/*/*/*.parquet' WHERE gap_full = 1"
        ).fetchone()[0]
    finally:
        con.close()

    stats = TransitionStats(
        n_rows_total=int(n_rows_total), n_rows_gap_full=int(n_rows_gap_full)
    )

    by_split: dict[str, tuple[list[list[float]], list[list[float]], list[datetime]]] = {
        "train": ([], [], []),
        "valid": ([], [], []),
        "test": ([], [], []),
    }
    for row in raw_rows:
        ts: datetime | None = row[0]
        gap_cur: int | None = row[1]
        gap_next: int | None = row[2 + n_feat + n_tgt]
        next_ts: datetime | None = row[2 + n_feat + n_tgt + 1]
        if ts is None:
            continue
        # Drop-Regeln (s. Modul-Doku): nur wenn **beide** Rows (aktuelle +
        # nächste) gap_full=0 sind; nächste Row muss existieren.
        if gap_cur == 1:
            stats.n_drops_gap_current += 1
            continue
        if next_ts is None:
            stats.n_drops_missing_next += 1
            continue
        if gap_next in (1, None):
            stats.n_drops_gap_full_next += 1
            continue
        name = _split_of_ts(ts)
        if not split_test and name == "test":
            continue
        by_split[name][0].append(
            [float("nan") if v is None else float(v) for v in row[2 : 2 + n_feat]]
        )
        by_split[name][1].append(
            [
                float("nan") if v is None else float(v)
                for v in row[2 + n_feat : 2 + n_feat + n_tgt]
            ]
        )
        by_split[name][2].append(ts)

    splits: dict[str, Split] = {}
    for name in ("train", "valid", "test"):
        if not split_test and name == "test":
            continue
        feats_rows, tgts_rows, ts_rows = by_split[name]
        if not ts_rows:
            continue
        X_raw = np.asarray(feats_rows, dtype=np.float32)
        Y_raw = np.asarray(tgts_rows, dtype=np.float32)
        stats.split_sizes[name] = int(len(ts_rows))
        feat_mask = np.isnan(X_raw)
        tgt_mask = np.isnan(Y_raw)
        stats.n_imputed_cells += int(feat_mask.sum()) + int(tgt_mask.sum())
        X = _fill_nan_rows(X_raw, axis=0)
        Y = _fill_nan_rows(Y_raw, axis=0)
        splits[name] = Split(
            name=name,
            X=X,
            Y=Y,
            ts=list(ts_rows),
            feature_null_mask=feat_mask.astype(np.uint8),
            target_null_mask=tgt_mask.astype(np.uint8),
        )
    stats.n_transitions = int(sum(stats.split_sizes.values()))
    return splits, stats


def flag_columns() -> tuple[str, ...]:
    """Stable NULL-Flag-Spalten (Train/Valid/Test-übergreifend identisch).

    Um das Feature-Dimension **konsistent** über alle Splits zu halten
    (Keras-Modell + LGBM + Artefakt), werden **alle** Input-Spalten
    (``config.FEATURE_INPUTS``) + **alle** Target-Spalten
    (``config.ANALOG_TARGETS``) als Flag-Spalten angehängt — unabhängig
    davon, ob sie in einem Split tatsächlich NULL waren. Konstant-0-Flags
    sind für GBDT wirkungslos (kein Split möglich), für Keras
    (standardisiert → konstant) und dokumentiert unschädlich.
    """
    return tuple(f"{c}_null" for c in config.FEATURE_INPUTS) + tuple(
        f"{c}_tgt_null" for c in config.ANALOG_TARGETS
    )


def with_null_flags(split: Split) -> tuple[np.ndarray, list[str]]:
    """Fügt **alle** NULL-Flag-Spalten an ``X`` an (stable Dimension).

    Flag-Spalten (alle 0/1, float32, am Ende von X, s.
    :func:`flag_columns`):
    - ``{col}_null``: Input-Spalte ``col`` war in der Input-Row NULL.
    - ``{col}_tgt_null``: Target-Spalte ``col`` war in der Ziel-Row (t+1) NULL.

    Returns
    -------
    (X_ext, flag_names): ``X_ext`` = (n, n_feat + n_flags); ``flag_names`` =
    Flag-Spaltnamen (Reihenfolge = Position in X_ext ab Index n_feat).
    """
    names = list(flag_columns())
    feat_mask = split.feature_null_mask.astype(np.float32)  # (n, n_feat)
    n_analog = len(config.ANALOG_TARGETS)
    tgt_mask = split.target_null_mask[:, :n_analog].astype(np.float32)  # Analog-Targets
    flags = np.concatenate([feat_mask, tgt_mask], axis=1)
    X_ext = np.concatenate([split.X, flags], axis=1)
    return X_ext, names


def extended_raw(split: Split) -> np.ndarray:
    """Raw-Extended-Matrix: ``[X (Rohfeatures), feature_null_mask,
    target_null_mask (Analog-Targets)]`` — dieselbe Spalten-Reihenfolge wie
    :func:`with_null_flags` (Flag-Columns werden im Rollout beim t0-Wert
    gehalten, s. :mod:`m2.rollout`)."""
    n_analog = len(config.ANALOG_TARGETS)
    return np.concatenate(
        [split.X, split.feature_null_mask.astype(np.float32),
         split.target_null_mask[:, :n_analog].astype(np.float32)],
        axis=1,
    )


def _fill_nan_rows(a: np.ndarray, axis: int) -> np.ndarray:
    """Forward- dann Backward-Fill von NaN entlang ``axis`` (Index-Nachbar =
    Zeitnachbar, Raster lückenlos + sortiert). Rest-NULLs → Spalten-Median
    (über gültige Values), falls leer: 0.0."""
    out = np.array(a, dtype=np.float64, copy=True)
    n = out.shape[axis]
    for i in range(1, n):
        prev = [slice(None)] * out.ndim
        cur = [slice(None)] * out.ndim
        prev[axis] = i - 1
        cur[axis] = i
        mask = np.isnan(out[tuple(cur)])
        if mask.any():
            out[tuple(cur)][mask] = out[tuple(prev)][mask]
    for i in range(n - 2, -1, -1):
        nxt = [slice(None)] * out.ndim
        cur = [slice(None)] * out.ndim
        nxt[axis] = i + 1
        cur[axis] = i
        mask = np.isnan(out[tuple(cur)])
        if mask.any():
            out[tuple(cur)][mask] = out[tuple(nxt)][mask]
    still = np.isnan(out)
    if still.any():
        other = tuple(d for d in range(out.ndim) if d != axis)
        med = np.nanmedian(out, axis=other, keepdims=True)
        med = np.where(np.isnan(med), 0.0, med)
        out[still] = np.broadcast_to(med, out.shape)[still]
    return out.astype(np.float32)


def assert_no_leakage(*splits: Split) -> None:
    """Walk-forward-Gate: ``max(train ts) < min(valid ts) < min(test ts)``."""
    names = [s.name for s in splits]
    if "train" not in names:
        raise ValueError("train-Split fehlt")
    train = next(s for s in splits if s.name == "train")
    valid = next((s for s in splits if s.name == "valid"), None)
    test = next((s for s in splits if s.name == "test"), None)
    if valid is None:
        raise ValueError("valid-Split fehlt")
    if not max(train.ts) < min(valid.ts):
        raise AssertionError("max(train ts) >= min(valid ts)")
    if test is not None and not max(valid.ts) < min(test.ts):
        raise AssertionError("max(valid ts) >= min(test ts)")


def load_splits(
    features_dir: Path | None = None,
    split_test: bool = True,
) -> tuple[dict[str, Split], TransitionStats]:
    """Alias für :func:`build_transitions` (API-Stabilität für Tests/CLI)."""
    return build_transitions(features_dir=features_dir, split_test=split_test)
