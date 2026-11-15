"""M2b — Daten: Feature-Store (v003: m1.3) → Spalten-Arrays, Targets je Horizont.

Anders als M2 (Transition-Liste mit Imputation) arbeitet M2b auf dem
**kompletten, lückenlosen Stunden-Raster** als Spalten-Arrays:

- Input-Zeile = Stunde ``t`` (NaN bleiben NaN — LightGBM verarbeitet fehlende
  Werte nativ; Keras-v2 imputiert selbst, s. :mod:`m2b.keras_model`).
- Ziel-Zeile = Stunde ``t+h`` = Index ``i+h`` (Raster lückenlos, M1-SCHEMA §3;
  an DST-Tagen ist ein Rasterschritt ≠ 60 min Wallclock — gezählt in
  :attr:`Dataset.n_nonhourly_steps`, wie in M2 akzeptiert).
- Target-Maske (je Target × Horizont): ``gap_full`` = 0 bei ``t`` **und**
  ``t+h``, Wert bei ``t`` und ``t+h`` endlich, RAS zusätzlich nicht
  „eingefroren“ (``ras_age`` ≤ ``RAS_STALE_AGE`` bei ``t`` und ``t+h``),
  **Purge**: Ziel-Zeit liegt im selben Split wie die Input-Zeit (kein
  Hineinragen von Train-Targets in 2025 bzw. Valid-Targets in 2026).
- Splits: Train ``TRAIN_START ≤ ts < 2025-01-01`` · Valid 2025 · Test
  ``ts ≥ 2026-01-01``; Stunden vor ``TRAIN_START`` = Label ``'pre'`` (ungenutzt,
  Regime-Wechsel 2021-10-20, s. ``config.TRAIN_START``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np

from . import config

SPLITS: tuple[str, ...] = ("train", "valid", "test")


def _to_float(v) -> np.ndarray:
    """DuckDB-NumPy-Spalte (ggf. masked) → float64 mit NaN für NULL."""
    if hasattr(v, "filled"):
        return np.asarray(v.astype(np.float64).filled(np.nan))
    return np.asarray(v, dtype=np.float64)


def load_store(
    features_dir: Path | None = None,
    columns: tuple[str, ...] | list[str] | None = None,
    where: str | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray], str]:
    """Lädt den Feature-Store (sortiert nach ``ts``).

    Returns ``(ts[datetime64[h]], {col: float64-Array}, data_version)``.
    Fehlende Spalten → ``KeyError`` mit Liste (früher Fehler statt stiller NaN).
    ``where`` = optionale SQL-Bedingung (z. B. eine einzelne Stunde).
    """
    feats = Path(features_dir) if features_dir is not None else config.FEATURES_DIR
    glob = f"{feats}/*/*/*.parquet"
    cols = list(dict.fromkeys(columns if columns is not None else _needed_columns()))
    con = duckdb.connect()
    try:
        avail = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{glob}'").fetchall()}
        missing = [c for c in cols if c not in avail]
        if missing:
            raise KeyError(f"Feature-Store ohne Spalten (m1.1 gebaut?): {missing[:12]} … ({len(missing)})")
        dv = "unknown"
        if "data_version" in avail:
            row = con.execute(f"SELECT max(data_version) FROM '{glob}'").fetchone()
            dv = str(row[0]) if row and row[0] is not None else "unknown"
        cond = f" WHERE {where}" if where else ""
        res = con.execute(
            f"SELECT ts, {', '.join(cols)} FROM '{glob}'{cond} ORDER BY ts"
        ).fetchnumpy()
    finally:
        con.close()
    ts = np.asarray(res.pop("ts")).astype("datetime64[h]")
    data = {k: _to_float(v) for k, v in res.items()}
    return ts, data, dv


def _needed_columns() -> list[str]:
    cols = list(config.RAW_INPUTS) + list(config.TARGETS) + list(config.TARGET_AUX)
    return list(dict.fromkeys(cols))


def shift(a: np.ndarray, k: int) -> np.ndarray:
    """``out[i] = a[i+k]`` (NaN außerhalb). ``k`` > 0 = Zukunft, < 0 = Vergangenheit."""
    out = np.full(a.shape, np.nan, dtype=np.float64)
    n = len(a)
    if k >= 0:
        if k < n:
            out[: n - k] = a[k:]
    else:
        k = -k
        if k < n:
            out[k:] = a[: n - k]
    return out


def split_labels(ts: np.ndarray, train_start: str | None = None) -> np.ndarray:
    """Split-Label je Zeile ('pre'/'train'/'valid'/'test')."""
    v0 = np.datetime64(config.VALID_START, "h")
    t0 = np.datetime64(config.TEST_START, "h")
    lab = np.full(len(ts), "train", dtype=object)
    if train_start:
        lab[ts < np.datetime64(train_start.replace(" ", "T"), "h")] = "pre"
    lab[(ts >= v0) & (ts < t0)] = "valid"
    lab[ts >= t0] = "test"
    return lab


@dataclass
class TargetSpec:
    """Ein Target × Horizont: Modell-Label, Basiswert, Maske."""

    target: str
    h: int
    kind: str  # 'analog' (Delta) | 'digital' (Level)
    y: np.ndarray  # Modell-Label (Delta bzw. Level), NaN wo ungültig
    base: np.ndarray  # y(t) (Persistenz-Basis, absolute Einheit)
    y_abs: np.ndarray  # y(t+h) absolut
    mask: np.ndarray  # bool, gültige Zeilen

    def to_abs(self, pred: np.ndarray, rows: np.ndarray | None = None) -> np.ndarray:
        """Modell-Ausgabe → absolute Einheit (Delta + Basis bzw. Level)."""
        base = self.base if rows is None else self.base[rows]
        return pred + base if self.kind == "analog" else pred


@dataclass
class Dataset:
    """Spalten-Arrays des Rasters + Features + Target-Cache."""

    ts: np.ndarray
    raw: dict[str, np.ndarray]
    data_version: str
    split: np.ndarray
    X: np.ndarray | None = None
    feature_names: list[str] = field(default_factory=list)
    n_nonhourly_steps: int = 0
    train_start: str | None = None
    _cache: dict[tuple[str, int], TargetSpec] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.ts)

    def rows(self, split: str) -> np.ndarray:
        return self.split == split

    def target(self, name: str, h: int) -> TargetSpec:
        key = (name, int(h))
        if key not in self._cache:
            self._cache[key] = build_target(self, name, int(h))
        return self._cache[key]


def build_target(ds: Dataset, name: str, h: int) -> TargetSpec:
    """Label/Maske für ``name`` @ ``t+h`` (s. Modul-Doku)."""
    kind = config.TARGET_KIND[name]
    cur = ds.raw[name]
    fut = shift(cur, h)
    gap = ds.raw["gap_full"]
    m = (gap == 0) & (shift(gap, h) == 0) & np.isfinite(cur) & np.isfinite(fut)
    if name in config.ZONE_OF_RAS:
        age = ds.raw[f"ras_age_{config.ZONE_OF_RAS[name]}"]
        age_f = shift(age, h)
        with np.errstate(invalid="ignore"):
            m &= np.isfinite(age) & np.isfinite(age_f)
            m &= (age <= config.RAS_STALE_AGE) & (age_f <= config.RAS_STALE_AGE)
    # Purge: Ziel-Split == Input-Split.
    lab_f = np.empty(ds.n, dtype=object)
    lab_f[:] = None
    if h < ds.n:
        lab_f[: ds.n - h] = ds.split[h:]
    m &= lab_f == ds.split
    y = (fut - cur) if kind == "analog" else fut.copy()
    y = np.where(m, y, np.nan)
    return TargetSpec(target=name, h=h, kind=kind, y=y, base=cur, y_abs=fut, mask=m)


_DEFAULT = object()


def build_dataset(
    features_dir: Path | None = None,
    check_version: bool = True,
    train_start: str | None | object = _DEFAULT,
) -> Dataset:
    """Lädt den Store und baut Features (:func:`m2b.features.derive`)."""
    from .features import derive

    ts, raw, dv = load_store(features_dir)
    if check_version and dv != config.EXPECTED_DATA_VERSION:
        raise ValueError(
            f"Feature-Store data_version={dv!r}, erwartet {config.EXPECTED_DATA_VERSION!r} "
            "(M1 neu bauen: python -m analysis.model.m1 features)"
        )
    start = config.TRAIN_START if train_start is _DEFAULT else train_start
    ds = Dataset(ts=ts, raw=raw, data_version=dv, split=split_labels(ts, start))
    ds.train_start = start
    steps = np.diff(ts).astype(np.int64)
    ds.n_nonhourly_steps = int((steps != 1).sum())
    ds.X, ds.feature_names = derive(raw, ts)
    return ds
