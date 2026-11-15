"""Fixtures: synthetischer Feature-Store (Hive-Layout, Spalten m1.3) mit plausibler Dynamik."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.m2b import config
from analysis.model.m2b.data import _needed_columns

_LAG_RE = re.compile(r"^(.+)_lag(\d+)$")


def make_store(root: Path, start: str = "2024-10-01", end: str = "2026-02-15", seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    ts = np.arange(np.datetime64(start, "h"), np.datetime64(end, "h"), np.timedelta64(1, "h"))
    n = len(ts)
    hours = (ts - ts.astype("datetime64[D]")).astype(int)
    doy = (ts.astype("datetime64[D]") - ts.astype("datetime64[Y]")).astype(int)
    ext = 10 + 9 * np.sin(2 * np.pi * (doy - 110) / 365) + 3 * np.sin(2 * np.pi * (hours - 9) / 24) + rng.normal(0, 0.8, n)
    cols: dict[str, np.ndarray] = {}
    offs = {z: np.round(rng.choice([0.0, 0.5, 1.0, 3.4], size=n, p=[0.6, 0.2, 0.15, 0.05]), 1) for z in config.ZONES}
    for z in config.ZONES:
        offs[z] = np.repeat(offs[z][::24], 24)[:n]
    ist = {z: np.zeros(n) for z in config.ZONES}
    pump = np.zeros(n)
    buff = np.zeros(n)
    b = 50.0
    temps = {z: 20.0 for z in config.ZONES}
    for i in range(n):
        perc = {z: temps[z] + offs[z][i] for z in config.ZONES}
        on = 1.0 if min(perc.values()) < 21.0 and ext[i] < 15 else 0.0
        pump[i] = on
        for z in config.ZONES:
            temps[z] += 0.25 * on - 0.03 * (temps[z] - ext[i]) / 10 + rng.normal(0, 0.03)
            ist[z][i] = temps[z]
        b += -1.5 * on + (2.0 if 10 <= hours[i] <= 15 else 0.0) * (ext[i] > 8) + rng.normal(0, 0.3)
        b = float(np.clip(b, 25, 80))
        buff[i] = b
    for z in config.ZONES:
        cols[f"ras_{z}_ist"] = ist[z]
        cols[f"offset_{z}"] = offs[z]
        cols[f"ras_{z}_perc"] = ist[z] + offs[z]
        cols[f"ras_age_{z}"] = rng.uniform(0, 1.2, n)
        # Rücklaufminderer der Etage: offen, wenn perzipiert < 21 °C (Heizsaison)
        v_on = ((ist[z] + offs[z] < 21.0) & (ext < 15)).astype(float)
        cols[f"hzrkl_{z}_on"] = v_on
        cols[f"hzrkl_{z}_last"] = v_on
        cols[f"hzrkl_{z}_pos"] = 20 + 30 * v_on + rng.normal(0, 2, n)
        # m1.3: Minderer-Regel (offen < 20,1 °C perzipiert, sonst zu) ≈ gemessenes Ventil
        rule = (ist[z] + offs[z] < config.HZRKL_RULE_THRESHOLD).astype(float)
        cols[f"hzrkl_{z}_rule"] = rule
        cols[f"hzrkl_{z}_rule_last"] = rule
    cols["ras_age_kell"][100:110] = 25.0  # Funkausfall → eingefroren
    cols["ext"] = ext
    cols["pump_hz_on"] = pump
    cols["pump_hz_last"] = pump
    cols["buff_top"] = buff
    cols["buff_mid"] = buff - 8 + rng.normal(0, 0.5, n)
    cols["buff_bot"] = buff - 15 + rng.normal(0, 0.5, n)
    cols["hz_vl"] = 25 + 20 * pump + rng.normal(0, 1, n)
    cols["hz_rl"] = cols["hz_vl"] - 5
    cols["o9_switch_margin"] = cols["buff_mid"] - cols["hz_rl"] - 5.0
    cols["gap_full"] = np.zeros(n)
    cols["gap_full"][500:503] = 1
    cols["gap_partial"] = np.zeros(n)
    for c in _needed_columns():
        if c in cols:
            continue
        m = _LAG_RE.match(c)
        if m and m.group(1) in cols:
            k = int(m.group(2))
            v = np.full(n, np.nan)
            v[k:] = cols[m.group(1)][:-k]
            cols[c] = v
        elif c.endswith("_last") and c[:-5] in cols:
            cols[c] = cols[c[:-5]] + rng.normal(0, 0.05, n)
        elif c.endswith(("_on", "_last")) or c in ("is_weekend", "is_holiday", "is_dst", "wx_is_forecast", "offset_known"):
            cols[c] = (rng.random(n) < 0.3).astype(float)
        elif c == "hour":
            cols[c] = hours.astype(float)
        elif c == "day_of_week":
            cols[c] = ((ts.astype("datetime64[D]").astype(int) + 3) % 7).astype(float)
        elif c.startswith("wx_t") and c.endswith("_t_bias"):
            k = int(c[4:].split("_")[0])
            cols[c] = np.roll(ext, -k)
        else:
            cols[c] = rng.normal(20, 5, n)
    # m1.3-Laufzeitfenster aus den (synthetischen) Signalen: Summe inkl. aktueller Stunde,
    # Stunden seit letzter Aktivität (> 0,05), 49 = in 49 h nichts aktiv.
    for sig in config.RUNTIME_SIGNALS:
        x = cols[sig]
        cs = np.concatenate([[0.0], np.cumsum(x)])
        for w in config.RUNTIME_WINDOWS:
            lo = np.maximum(np.arange(n) + 1 - w, 0)
            cols[f"{sig}_sum{w}h"] = cs[np.arange(n) + 1] - cs[lo]
        since = np.full(n, 49.0)
        last = -10**9
        for i in range(n):
            if x[i] > 0.05:
                last = i
            if i - last <= 48:
                since[i] = float(i - last)
        cols[f"{sig}_since_h"] = since
    # Lags für die explizit gesetzten Spalten nachziehen (Reihenfolge-unabhängig):
    for c in _needed_columns():
        m = _LAG_RE.match(c)
        if m and m.group(1) in cols:
            k = int(m.group(2))
            v = np.full(n, np.nan)
            v[k:] = cols[m.group(1)][:-k]
            cols[c] = v
    months = ts.astype("datetime64[M]")
    for mth in np.unique(months):
        sel = months == mth
        y, mm = str(mth)[:4], str(mth)[5:7]
        tbl = {"ts": pa.array(ts[sel].astype("datetime64[us]"))}
        tbl |= {c: pa.array(v[sel].astype(np.float64)) for c, v in cols.items()}
        tbl["data_version"] = pa.array([config.EXPECTED_DATA_VERSION] * int(sel.sum()))
        p = root / y / mm / f"{y}-{mm}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(tbl), p)
    return root


@pytest.fixture(scope="session")
def store(tmp_path_factory) -> Path:
    return make_store(tmp_path_factory.mktemp("features_m13"))


@pytest.fixture(scope="session")
def ds(store):
    from analysis.model.m2b.data import build_dataset

    return build_dataset(store)
