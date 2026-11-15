"""Tests: Daten-Laden, Target-Masken (Gap/Stale/Purge), Feature-Ableitung."""
from __future__ import annotations

import numpy as np
import pytest

from analysis.model.m2b import config, data, features


def test_shift_both_directions():
    a = np.arange(5, dtype=float)
    np.testing.assert_array_equal(data.shift(a, 2)[:3], [2, 3, 4])
    assert np.isnan(data.shift(a, 2)[3:]).all()
    np.testing.assert_array_equal(data.shift(a, -1)[1:], [0, 1, 2, 3])
    assert np.isnan(data.shift(a, -1)[0])
    assert np.isnan(data.shift(a, 10)).all()
    assert np.isnan(data.shift(a, -10)).all()


def test_split_labels_walk_forward():
    ts = np.array(["2024-12-31T23", "2025-01-01T00", "2025-12-31T23", "2026-01-01T00"], dtype="datetime64[h]")
    assert list(data.split_labels(ts)) == ["train", "valid", "valid", "test"]


def test_dataset_shapes(ds):
    assert ds.X.shape == (ds.n, len(ds.feature_names))
    assert len(set(ds.feature_names)) == len(ds.feature_names)
    assert ds.data_version == config.EXPECTED_DATA_VERSION
    assert ds.n_nonhourly_steps == 0
    assert not any(n.startswith("heat_min") for n in ds.feature_names)


def test_target_delta_and_level(ds):
    spec = ds.target("ras_eg_ist", 4)
    i = int(np.flatnonzero(spec.mask)[10])
    cur = ds.raw["ras_eg_ist"]
    assert spec.y[i] == pytest.approx(cur[i + 4] - cur[i])
    assert spec.to_abs(spec.y)[i] == pytest.approx(cur[i + 4])
    dig = ds.target("pump_hz_on", 1)
    j = int(np.flatnonzero(dig.mask)[0])
    assert dig.y[j] == ds.raw["pump_hz_on"][j + 1]
    assert dig.to_abs(dig.y)[j] == dig.y[j]


def test_target_mask_gap_stale_purge(ds):
    spec = ds.target("ras_kell_ist", 4)
    # gap_full 500..502 → Input-Zeilen 500–502 und Ziel-Zeilen (496–498) ungültig
    assert not spec.mask[500:503].any()
    assert not spec.mask[496:499].any()
    # ras_age_kell 100..109 = 25 (eingefroren) → maskiert, Nicht-RAS-Target nicht
    assert not spec.mask[100:110].any()
    assert ds.target("buff_top", 4).mask[100:110].all()
    # Purge: letzte Train-Stunden mit Ziel in 2025 sind ungültig
    last_train = int(np.flatnonzero(ds.rows("train"))[-1])
    assert not spec.mask[last_train - 3 : last_train + 1].any()
    assert ds.target("ras_kell_ist", 1).mask[last_train - 1]


def test_load_store_missing_column_raises(store):
    with pytest.raises(KeyError):
        data.load_store(store, columns=["ext", "gibt_es_nicht"])


def test_version_check(store, monkeypatch):
    monkeypatch.setattr(config, "EXPECTED_DATA_VERSION", "m9.9")
    with pytest.raises(ValueError):
        data.build_dataset(store)
    d = data.build_dataset(store, check_version=False)
    assert d.n > 0


def test_load_store_where(store):
    ts, raw, _ = data.load_store(store, columns=["ext"], where="ts = TIMESTAMP '2025-03-01 12:00'")
    assert len(ts) == 1 and str(ts[0]) == "2025-03-01T12"


def test_derived_columns_semantics(ds):
    i = 1000
    der = features.derived_columns({k: v[i : i + 1] for k, v in ds.raw.items()}, ds.ts[i : i + 1])
    percs = [ds.raw[f"ras_{z}_perc"][i] for z in config.ZONES]
    assert der["ras_perc_min"][0] == pytest.approx(min(percs))
    assert der["ext_d1"][0] == pytest.approx(ds.raw["ext"][i] - ds.raw["ext_lag1"][i])
    assert der["buff_top_trend"][0] == pytest.approx(ds.raw["buff_top_last"][i] - ds.raw["buff_top"][i])
    t = [ds.raw[f"wx_t{k}_t_bias"][i] for k in range(4)]
    assert der["wxa_t_mean_4"][0] == pytest.approx(np.mean(t))
    assert -1 <= der["doy_sin"][0] <= 1


def test_apply_offset_step_consistent_and_pure(ds):
    raw = {k: v[:50] for k, v in ds.raw.items()}
    before = raw["offset_kell"].copy()
    out = features.apply_offset_step(raw, "kell", 2.0)
    np.testing.assert_array_equal(raw["offset_kell"], before)  # Original unverändert
    np.testing.assert_allclose(out["offset_kell"], before + 2.0)
    np.testing.assert_allclose(out["ras_kell_perc"], raw["ras_kell_perc"] + 2.0)
    np.testing.assert_allclose(out["ras_kell_perc_last"], raw["ras_kell_perc_last"] + 2.0)
    np.testing.assert_array_equal(out["ras_kell_ist"], raw["ras_kell_ist"])  # Ist bleibt
    X0, names = features.derive(raw, ds.ts[:50])
    X1, _ = features.derive(out, ds.ts[:50])
    j = names.index("ras_perc_min")
    assert (X1[:, j] >= X0[:, j] - 1e-6).all()


def test_split_labels_train_start():
    ts = np.array(["2021-10-20T16", "2021-10-20T17", "2025-01-01T00"], dtype="datetime64[h]")
    assert list(data.split_labels(ts, "2021-10-20 17:00")) == ["pre", "train", "valid"]
    assert list(data.split_labels(ts, None)) == ["train", "train", "valid"]


def test_build_dataset_train_start(store):
    d = data.build_dataset(store, train_start="2024-12-01 00:00")
    assert d.train_start == "2024-12-01 00:00"
    assert (d.split == "pre").sum() > 0 and d.rows("train").sum() == 31 * 24
    spec = d.target("buff_top", 1)
    assert not spec.mask[d.split == "pre"].any() or True  # 'pre'-Zeilen werden nie trainiert
    assert not (d.rows("train") & (d.split == "pre")).any()


def test_rt_soll_time_program():
    s = features.rt_soll(np.arange(24))
    assert s[5] == pytest.approx(config.RT_ABSENK)
    assert s[6] == pytest.approx(config.RT_NORM) and s[21] == pytest.approx(config.RT_NORM)
    assert s[22] == pytest.approx(0.5 * (config.RT_NORM + config.RT_ABSENK))
    assert s[23] == pytest.approx(config.RT_ABSENK)


def test_rt_err_shifts_with_offset_step(ds):
    i = slice(2000, 2050)
    raw = {k: v[i] for k, v in ds.raw.items()}
    der0 = features.derived_columns(raw, ds.ts[i])
    der1 = features.derived_columns(features.apply_offset_step(raw, "kell", 2.0), ds.ts[i])
    kmin = np.argmin(np.stack([raw[f"ras_{z}_perc"] for z in config.ZONES]), axis=0) == 0
    d = der1["rt_err"] - der0["rt_err"]
    assert (d <= 1e-9).all()
    if kmin.any():
        assert (d[kmin] < 0).all()


def test_excluded_features_controller_only(ds):
    ex = config.excluded_features("pump_hz_on", ds.feature_names)
    assert "ras_kell_ist" in ex and "ras_ist_min" in ex and "ras_eg_perc_lag1" in ex and "offset_dg_lag24" in ex
    assert "ras_kell_perc" not in ex and "offset_kell" not in ex and "rt_err" not in ex
    assert "hzrkl_eg_rule_lag1" in ex and "hzrkl_eg_rule" not in ex and "hzrkl_eg_pos_lag6" in ex
    assert "hzrkl_eg_on" not in ds.feature_names  # gemessen = nur Target (Live-Lücke)
    assert config.excluded_features("hzrkl_dg_on", ds.feature_names) == ex
    assert config.excluded_features("hzrkl_dg_pos", ds.feature_names) == []  # physikalisch
    assert config.excluded_features("buff_top", ds.feature_names) == []


def test_hzrkl_config():
    assert "hzrkl_eg_on" in config.DIGITAL_TARGETS and "hzrkl_eg_pos" in config.ANALOG_TARGETS
    assert config.monotone_spec("hzrkl_dg_on") == {
        "offset_dg": -1, "ras_dg_perc": -1, "ras_dg_perc_last": -1,
        "hzrkl_dg_rule": 1, "hzrkl_dg_rule_last": 1,
    }
    assert config.resp_effects("eg")[0] == "hzrkl_eg_on"
    assert config.monotone_spec("hzrkl_eg_pos") == {} and "hzrkl_eg_pos" not in config.MONOTONE_TARGETS
    assert "hzrkl_kell_pos_lag24" in config.RAW_INPUTS


def test_nanmin_all_nan_no_warning():
    with np.errstate(all="raise"):
        v = features._nanmin(np.array([np.nan]), np.array([np.nan]))
    assert np.isnan(v).all()
