"""Tests v003: UVR-Margen/Heizkurve, Minderer-Regel im Offset-Schritt, derive(names),
Versionssperre im Predictor, excluded/monotone für die Regel, Report-Referenz v002."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from analysis.model.m2b import __main__ as cli
from analysis.model.m2b import config, features, lgbm, report, responsivity
from analysis.model.m2b.pipeline import run_train
from analysis.model.m2b.predict import L1Predictor, artifact_data_version, check_data_version

_V002 = config.ARTIFACT_V002_DIR / "features.json"


def _raw_f3(**kw) -> dict[str, np.ndarray]:
    base = {
        "buff_top": [60.0], "buff_mid": [50.0], "buff_bot": [40.0],
        "hz_vl": [45.0], "hz_rl": [35.0], "oil_vl": [90.0], "wood_vl": [70.0],
        "ext": [0.0], "pump_hz_on": [0.5], "valve_hzrl_on": [1.0],
        "buff_top_last": [61.0], "buff_mid_last": [44.0], "buff_bot_last": [68.0],
        "hz_rl_last": [30.0], "oil_vl_last": [80.0], "wood_vl_last": [60.0],
    }
    base.update(kw)
    return {k: np.asarray(v, dtype=np.float64) for k, v in base.items()}


def _ts(n: int = 1) -> np.ndarray:
    return np.arange(np.datetime64("2025-01-10T12", "h"), np.datetime64("2025-01-10T12", "h") + n)


def test_uvr_margins_and_proxies():
    X, names = features.derive(_raw_f3(), _ts(), names=features.DERIVED_F3)
    v = dict(zip(names, X[0].astype(float), strict=True))
    assert v["uvr_o9_margin"] == pytest.approx(50 - 35 - 5)
    assert v["uvr_o9_margin_last"] == pytest.approx(44 - 30 - 5)
    assert v["uvr_o2_margin"] == pytest.approx(90 - 60 - 24.6, abs=1e-4)
    assert v["uvr_o2_margin_last"] == pytest.approx(80 - 61 - 24.6, abs=1e-4)
    assert v["uvr_ww_margin"] == pytest.approx(57 - 60)
    assert v["uvr_ww_margin_last"] == pytest.approx(57 - 61)
    # O1: min(wood_vl − 65, wood_vl − buff_bot − 3)
    assert v["uvr_o1_margin"] == pytest.approx(min(70 - 65, 70 - 40 - 3))
    assert v["uvr_o1_margin_last"] == pytest.approx(min(60 - 65, 60 - 68 - 3))
    assert v["buff_strat_top_mid"] == pytest.approx(10) and v["buff_strat_mid_bot"] == pytest.approx(10)
    assert v["hz_flow_pump"] == pytest.approx(5.0) and v["hz_flow_o9"] == pytest.approx(10.0)
    assert v["vlst_est"] == pytest.approx(45 + 10 * 2 / 3, abs=1e-4)
    assert v["vl_err"] == pytest.approx(v["vlst_est"] - 45, abs=1e-4)
    # NULL bleibt NULL (LightGBM: fehlt)
    Xn, _ = features.derive(_raw_f3(oil_vl=[np.nan]), _ts(), names=["uvr_o2_margin"])
    assert np.isnan(Xn[0, 0])


def test_vlst_est_curve_and_clamp():
    ext = np.array([10.0, -20.0, -5.0, 40.0, 55.0, -27.5, -40.0, np.nan])
    v = features.vlst_est(ext)
    np.testing.assert_allclose(v[:3], [45.0, 65.0, 55.0])
    assert v[3] == pytest.approx(config.HK_VL_MIN) and v[4] == pytest.approx(config.HK_VL_MIN)  # unten geklemmt
    assert v[5] == pytest.approx(config.HK_VL_MAX) and v[6] == pytest.approx(config.HK_VL_MAX)  # oben geklemmt
    assert np.isnan(v[7])
    assert ((v[:7] >= 25) & (v[:7] <= 70)).all()


def _rule_raw(perc_last, rule_last, rule):
    n = len(perc_last)
    return {
        "offset_eg": np.zeros(n), "ras_eg_perc": np.asarray(perc_last, dtype=float),
        "ras_eg_perc_last": np.asarray(perc_last, dtype=float),
        "hzrkl_eg_rule_last": np.asarray(rule_last, dtype=float),
        "hzrkl_eg_rule": np.asarray(rule, dtype=float),
    }


def test_offset_step_flips_rule_and_holds_on_tie():
    raw = _rule_raw([19.5, 19.5, 19.1, 20.5, np.nan, 18.0], [1, 1, 0, 0, 1, 1], [1, 0.6, 0.3, 0, 0.4, 0.8])
    out = features.apply_offset_step(raw, "eg", 1.0)
    # 20,5 > 20,1 → zu; 19,1 + 1 = 20,1 (Gleichstand) → bisheriger Zustand; NULL → halten; 19,0 → offen
    np.testing.assert_array_equal(out["hzrkl_eg_rule_last"], [0, 0, 0, 0, 1, 1])
    np.testing.assert_allclose(out["hzrkl_eg_rule"], [0, 0, 0.3, 0, 0.4, 0.8])
    np.testing.assert_allclose(raw["hzrkl_eg_rule"], [1, 0.6, 0.3, 0, 0.4, 0.8])  # Original unverändert
    down = features.apply_offset_step(raw, "eg", -1.0)  # 20,5 − 1 = 19,5 → öffnet
    assert down["hzrkl_eg_rule_last"][3] == 1.0 and down["hzrkl_eg_rule"][3] == 1.0
    tie = features.apply_offset_step(_rule_raw([20.1], [1], [0.7]), "eg", 0.0)
    assert tie["hzrkl_eg_rule_last"][0] == 1.0 and tie["hzrkl_eg_rule"][0] == pytest.approx(0.7)
    tie0 = features.apply_offset_step(_rule_raw([20.1], [0], [0.2]), "eg", 0.0)
    assert tie0["hzrkl_eg_rule_last"][0] == 0.0


def test_offset_step_zero_is_identity_on_store(ds):
    raw = {k: v[:300] for k, v in ds.raw.items()}
    out = features.apply_offset_step(raw, "kell", 0.0)
    np.testing.assert_array_equal(out["hzrkl_kell_rule"], raw["hzrkl_kell_rule"])
    np.testing.assert_array_equal(out["hzrkl_kell_rule_last"], raw["hzrkl_kell_rule_last"])
    up = features.apply_offset_step(raw, "kell", 5.0)
    assert (up["hzrkl_kell_rule_last"] <= raw["hzrkl_kell_rule_last"]).all()  # nur Richtung „zu“


def test_offset_step_without_rule_columns_v002():
    raw = {"offset_eg": np.zeros(2), "ras_eg_perc": np.array([19.0, 21.0]), "ras_eg_perc_last": np.array([19.0, 21.0])}
    out = features.apply_offset_step(raw, "eg", 2.0)
    assert set(out) == set(raw)
    np.testing.assert_allclose(out["ras_eg_perc_last"], [21.0, 23.0])


def test_rule_state():
    s = features.rule_state(np.array([19.0, 20.1, 21.0, np.nan]), np.array([0.0, 0.0, 1.0, 1.0]))
    np.testing.assert_array_equal(s, [1.0, 0.0, 0.0, 1.0])


def test_derive_names_subset_order_and_keyerror(ds):
    raw = {k: v[:40] for k, v in ds.raw.items()}
    ts = ds.ts[:40]
    X, names = features.derive(raw, ts)
    assert names == features.default_feature_names() == ds.feature_names
    sub = ["rt_err", names[5], "uvr_o2_margin_last", "hzrkl_eg_rule", names[0], "vlst_est"]
    Xs, ns = features.derive(raw, ts, names=sub)
    assert ns == sub and Xs.shape == (40, len(sub))
    for j, n in enumerate(sub):
        np.testing.assert_array_equal(Xs[:, j], X[:, names.index(n)])
    with pytest.raises(KeyError):
        features.derive(raw, ts, names=["rt_err", "gibt_es_nicht"])
    X0, n0 = features.derive(raw, ts, names=[])
    assert X0.shape == (40, 0) and n0 == []
    assert not set(config.RAW_INPUTS) & set(features.DERIVED_F2 + features.DERIVED_F3)


def test_required_raw():
    req = features.required_raw(["rt_err", "uvr_o1_margin", "ext"])
    assert set(req) == {f"ras_{z}_perc" for z in config.ZONES} | {"wood_vl", "buff_bot", "ext"}
    assert features.required_raw(list(config.RAW_INPUTS)) == list(config.RAW_INPUTS)


@pytest.mark.skipif(not _V002.exists(), reason="v002-Artefakt fehlt")
def test_v002_feature_names_derivable():
    f = json.loads(_V002.read_text())
    n_raw = len(f["raw_inputs"])
    assert f["feature_names"][n_raw:] == features.DERIVED_F2  # m2b-f2 unverändert, gleiche Reihenfolge
    assert set(features.required_raw(f["feature_names"])) == set(f["raw_inputs"])
    assert "hzrkl_eg_on" in f["raw_inputs"] and "hzrkl_eg_on" not in config.RAW_INPUTS


def test_config_v003_inputs():
    ri = set(config.RAW_INPUTS)
    assert config.ARTIFACT_NAME == "cmi-brain-v003" and config.ARTIFACT_DIR.name == "v003"
    assert config.ARTIFACT_V002_DIR.name == "v002" and config.EXPECTED_DATA_VERSION == "m1.3"
    for z in config.ZONES:
        assert f"hzrkl_{z}_on" not in ri and f"hzrkl_{z}_last" not in ri
        assert not any(c.startswith(f"hzrkl_{z}_on_lag") for c in ri)
        assert {f"hzrkl_{z}_rule", f"hzrkl_{z}_rule_last", f"hzrkl_{z}_rule_lag24"} <= ri
        assert {f"hzrkl_{z}_pos", f"hzrkl_{z}_pos_last", f"hzrkl_{z}_pos_lag1"} <= ri
        assert f"hzrkl_{z}_on" in config.TARGETS
    assert set(config.RUNTIME_INPUTS) <= ri and len(config.RUNTIME_INPUTS) == 9 * 5
    assert {"pump_wood_est_on", "wood_fire_on", "o9_switch_margin"} <= ri
    assert set(config.CURRENT_ON_INPUTS) <= ri
    assert len(config.RAW_INPUTS) == len(ri)  # keine Duplikate
    assert set(config.V003_NEW_RAW_INPUTS) <= ri


def test_excluded_and_monotone_for_rule(ds):
    fn = ds.feature_names
    for t in config.CONTROLLER_TARGETS:
        ex = set(config.excluded_features(t, fn))
        assert {"hzrkl_eg_rule_lag1", "hzrkl_dg_rule_lag24", "oel_burner_on_sum3h",
                "pump_hz_on_sum24h", "valve_hzrl_on_since_h", "pump_wood_est_on_since_h"} <= ex
        assert not {"hzrkl_eg_rule", "hzrkl_eg_rule_last", "o9_switch_margin", "pump_hz_on"} & ex
    assert config.excluded_features("buff_mid", fn) == []
    for z in config.ZONES:
        spec = config.monotone_spec(f"hzrkl_{z}_on")
        assert spec[f"hzrkl_{z}_rule"] == 1 and spec[f"hzrkl_{z}_rule_last"] == 1
        assert spec[f"ras_{z}_perc"] == -1
        v = lgbm.monotone_vector(f"hzrkl_{z}_on", fn)
        assert v[fn.index(f"hzrkl_{z}_rule")] == 1 and v[fn.index(f"hzrkl_{z}_rule_last")] == 1
    assert lgbm.monotone_vector("hzrkl_eg_on", fn)[fn.index("hzrkl_kell_rule")] == 0  # fremde Etage frei
    assert "hzrkl_eg_rule" not in config.monotone_spec("pump_hz_on")


def test_responsivity_uses_rule(ds):
    import copy

    fn = ds.feature_names
    # Synthetik liegt meist über 20,1 °C (Regel zu) → EG um 1,5 K kälter, Regel neu bestimmen.
    d2 = copy.copy(ds)
    raw = dict(ds.raw)
    for c in ("ras_eg_perc", "ras_eg_perc_last"):
        raw[c] = ds.raw[c] - 1.5
    rule = features.rule_state(raw["ras_eg_perc_last"], np.zeros(ds.n))
    raw["hzrkl_eg_rule"], raw["hzrkl_eg_rule_last"] = rule, rule
    d2.raw = raw

    def pred(t, h, X):  # Spielzeug: Ventil = Regel-Zustand der Etage
        if t.startswith("hzrkl_") and t.endswith("_on"):
            return X[:, fn.index(f"hzrkl_{t.split('_')[1]}_rule_last")].astype(float)
        return np.zeros(len(X))

    cases = responsivity.select_cases(d2, n_cases=60)
    assert rule[cases].mean() > 0.2  # genügend offene Fälle
    r = responsivity.run(d2, pred, cases=cases, available={"hzrkl_eg_on"})
    d = np.array([r.per_delta[k]["mean_d_hzrkl_eg_on"] for k in r.per_delta])
    assert (d <= 0).all() and d[-1] < 0  # Schritt nach oben schließt nur
    assert d[-1] <= d[0]  # größerer Schritt schließt mindestens so viele


def test_importance_share(ds):
    import lightgbm as lgb

    X = ds.X[:400]
    y = X[:, ds.feature_names.index("buff_top")]
    b = lgb.train({"objective": "l2", "verbosity": -1, "num_threads": 2, "seed": 1},
                  lgb.Dataset(X, label=y, feature_name=ds.feature_names), num_boost_round=5)
    tot, top = lgbm.importance_share(b, ds.feature_names, ["buff_top", "ext"], top=1)
    assert 0.0 < tot <= 1.0 and top[0][0] == "buff_top"
    assert lgbm.importance_share(b, ds.feature_names, [], top=3) == (0.0, [])


def test_reference_from_summary_scales_flue():
    s = {"artifact": "cmi-brain-v002", "data_version": "m1.2", "metrics": {
        "lgbm": {"flue@4": {"valid": {"mae": 0.5, "rmse": 1.0, "mae_b0": 0.6, "rmse_b0": None},
                            "test": {"mae": 0.4}},
                 "buff_mid@4": {"valid": {"mae": 1.7}}},
        "lgbm_refit": {"flue@4": {"test": {"mae": 0.45}}}},
        "responsivity": {"lgbm": {"eg": {"primary": "hzrkl_eg_on", "pearson_primary": -0.73,
                                         f"frac_neg_primary_at_{config.RESP_TARGET_DELTA}K": 1.0},
                                  "dod_zone": "eg"}}}
    r = report.reference_from_summary(s)
    assert r["metrics"]["flue@4"]["valid"]["mae"] == pytest.approx(5.0)
    assert r["metrics"]["flue@4"]["valid"]["rmse_b0"] is None
    assert r["metrics"]["flue@4"]["refit_test"]["mae"] == pytest.approx(4.5)
    assert r["metrics"]["buff_mid@4"]["valid"]["mae"] == pytest.approx(1.7)
    assert r["responsivity"]["lgbm"]["eg"]["pearson_primary"] == -0.73
    assert report.load_reference(None) is None
    assert report.load_reference(Path("/nicht/vorhanden")) is None
    assert report._pct(1.0, 0.0) == "–" and report._pct(0.9, 1.0) == "-10.0 %"


@pytest.fixture(scope="module")
def mini_art(store, tmp_path_factory):
    out = tmp_path_factory.mktemp("v003_mini")
    s = run_train(features_dir=store, out_dir=out, targets=("buff_top", "hzrkl_eg_on"), horizons=(1,),
                  threads=2, num_boost=15, with_keras=False, with_refit=False, resp_cases=10,
                  report_path=out / "R.md", reference_dir=None, log=lambda m: None)
    return out, s


def test_mini_artifact_summary(mini_art):
    out, s = mini_art
    assert s["reference_v002"] is None and s["n_raw_inputs"] == len(config.RAW_INPUTS)
    assert s["n_new_features"] > 50
    imp = s["importance_new"]["buff_top@1"]
    assert 0.0 <= imp["share_total"] <= 1.0 and len(imp["top"]) <= 10
    f = json.loads((out / "features.json").read_text())
    assert f["derive_version"] == features.DERIVE_VERSION == "m2b-f3"
    txt = (out / "R.md").read_text()
    assert "v003" in txt and "Neue v003-Merkmale" in txt


def test_predictor_version_lock(mini_art, store, tmp_path, capsys):
    out, _ = mini_art
    p = L1Predictor.load(out)
    assert p.data_version == config.EXPECTED_DATA_VERSION
    assert "hzrkl_eg_on" in p.store_columns() and "ras_eg_perc" in p.store_columns()
    res = p.predict_at("2025-02-10 18:00", features_dir=store, offset_delta={"eg": 2.0})
    assert set(res[1]) == {"buff_top", "hzrkl_eg_on"}
    # Artefakt „auf m1.2 trainiert“ → Store m1.3 abgelehnt, mit Hinweis auf den passenden Store
    meta = json.loads((out / "metadata.json").read_text())
    old = tmp_path / "old"
    old.mkdir()
    for name in ("features.json", "summary.json"):
        (old / name).write_text((out / name).read_text())
    (old / "lgbm").symlink_to(out / "lgbm")
    meta.pop("data_version")
    (old / "metadata.json").write_text(json.dumps(meta))
    summ = json.loads((old / "summary.json").read_text())
    summ.pop("data_version")
    (old / "summary.json").write_text(json.dumps(summ))
    p2 = L1Predictor.load(old)
    assert p2.data_version == "m1.2"  # Default ohne Angabe
    with pytest.raises(ValueError, match="features_m1.2"):
        p2.predict_at("2025-02-10 18:00", features_dir=store)
    assert p2.predict_at("2025-02-10 18:00", features_dir=store, check_version=False)[1]
    with pytest.raises(ValueError, match="features_m1.2"):
        cli.main(["predict", "--ts", "2025-02-10 18:00", "--model", str(old), "--features-dir", str(store)])
    assert cli.main(["predict", "--ts", "2025-02-10 18:00", "--model", str(old), "--features-dir", str(store),
                     "--no-version-check"]) == 0
    assert "Daten m1.2" in capsys.readouterr().out


def test_artifact_data_version_and_check():
    assert artifact_data_version({"data_version": "m1.3"}) == "m1.3"
    assert artifact_data_version({}, {"data_version": "m1.1"}) == "m1.1"
    assert artifact_data_version({}, None) == config.ARTIFACT_DATA_VERSION_DEFAULT == "m1.2"
    check_data_version("m1.3", "m1.3")
    with pytest.raises(ValueError, match="data/features`"):
        check_data_version("m1.2", "m1.3", "cmi-brain-v003")
    with pytest.raises(ValueError, match="trainiert auf 'm9'"):
        check_data_version("m1.3", "m9")
