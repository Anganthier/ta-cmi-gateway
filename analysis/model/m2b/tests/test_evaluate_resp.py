"""Tests: Metriken/Baselines/DoD + Responsivität mit Spielzeug-Prädiktoren."""
from __future__ import annotations

import numpy as np
import pytest

from analysis.model.m2b import config, evaluate, responsivity


def test_perfect_prediction_scores_zero(ds):
    spec = ds.target("buff_top", 4)
    r = evaluate.score(ds, spec, spec.y_abs.copy(), "valid")
    assert r["mae"] == pytest.approx(0.0)
    assert r["skill_b0"] == pytest.approx(1.0)
    assert r["n"] == int((ds.rows("valid") & spec.mask).sum())
    assert {"mae_b0", "mae_b0_last", "mae_b2", "max_abs_err", "max_abs_pred"} <= set(r)


def test_persistence_prediction_equals_b0(ds):
    spec = ds.target("ras_kell_ist", 4)
    r = evaluate.score(ds, spec, spec.base.copy(), "valid")
    assert r["mae"] == pytest.approx(r["mae_b0"])
    assert r["skill_b0"] == pytest.approx(0.0, abs=1e-12)


def test_baseline_b2_uses_yesterday(ds):
    spec = ds.target("ext", 1) if "ext" in config.TARGET_KIND else ds.target("hz_vl", 1)
    b = evaluate.baseline_preds(ds, spec)
    i = 100
    exp = 0.5 * spec.base[i] + 0.5 * ds.raw[spec.target][i + 1 - 24]
    assert b["b2"][i] == pytest.approx(exp)


def test_empty_split_nan():
    assert np.isnan(evaluate._mae(np.array([]), np.array([])))


def _fake_metrics(mae_ras: float, mae_buff: float) -> dict:
    m = {}
    for t in config.RAS_IST_TARGETS:
        m[f"{t}@4"] = {"valid": {"mae": mae_ras, "mae_b0": 0.2, "skill_b0": 1 - mae_ras / 0.2, "max_abs_pred": 22.0}}
    for t in config.BUFF_TARGETS:
        m[f"{t}@4"] = {"valid": {"mae": mae_buff, "mae_b0": 3.0, "skill_b0": 1 - mae_buff / 3.0, "max_abs_pred": 60.0}}
    return m


def test_dod_check_pass_and_fail():
    ok = evaluate.dod_check({"x": _fake_metrics(0.15, 1.2)}, "x")
    assert ok["ras_ok"] and ok["buff_ok"] and ok["no_explosion"]
    bad = evaluate.dod_check({"x": _fake_metrics(0.25, 1.8)}, "x")
    assert not bad["ras_ok"] and not bad["buff_ok"]
    miss = evaluate.dod_check({"x": {}}, "x")
    assert not miss["ras_ok"] and not miss["no_explosion"]


def test_mean_skill():
    m = _fake_metrics(0.1, 1.5)
    assert evaluate.mean_skill(m) == pytest.approx(np.mean([0.5] * 3 + [0.5] * 3))
    assert np.isnan(evaluate.mean_skill({}))


def test_select_cases_heating_only(ds):
    assert config.RESP_ZONE == "eg"  # Q-110: EG = größte Heizlast
    c = responsivity.select_cases(ds, n_cases=30)
    assert len(c) == 30
    assert (ds.raw["ext"][c] < config.RESP_HEATING_EXT_MAX).all()
    assert (ds.split[c] == "valid").all()
    np.testing.assert_array_equal(c, responsivity.select_cases(ds, n_cases=30))  # Seed fest


def _toy_pred(ds):
    """Spielzeug-Regler: Ventil der Etage = sigmoid(21 − perc_z), Pumpe = sigmoid(21 − perc_min)."""
    fn = ds.feature_names
    j = fn.index("ras_perc_min")

    def pred(t, h, X):
        if t.startswith("hzrkl_"):
            z = t.split("_")[1]
            p = X[:, fn.index(f"ras_{z}_perc")]
            s = 1.0 / (1.0 + np.exp(-(21.0 - p)))
            return s if t.endswith("_on") else 20 + 30 * s
        if t == "pump_hz_on":
            return 1.0 / (1.0 + np.exp(-(21.0 - X[:, j])))
        z = t.split("_")[1]
        return -0.05 * (X[:, fn.index(f"ras_{z}_perc")] - X[:, fn.index(f"ras_{z}_ist")])

    return pred


def test_responsivity_detects_negative_effect(ds):
    r = responsivity.run(ds, _toy_pred(ds), cases=responsivity.select_cases(ds, n_cases=40))
    d = r.as_dict()
    assert r.zone == "eg" and r.primary == "hzrkl_eg_on"
    assert r.corr_primary < 0 and r.corr["ras_ist"] < 0 and r.corr["hzrkl_eg_pos"] < 0
    assert d["dod_passed"] == r.passed
    assert d["pearson"]["pump_hz_on"] is not None
    assert set(d["per_delta"]) == {f"+{x:.1f}K" for x in config.RESP_DELTAS_K}


def test_responsivity_available_fallback_to_pump(ds):
    r = responsivity.run(ds, _toy_pred(ds), cases=responsivity.select_cases(ds, n_cases=20),
                         zone="kell", available={"pump_hz_on"})
    assert r.primary == "pump_hz_on" and "ras_ist" not in r.corr
    with pytest.raises(ValueError):
        responsivity.run(ds, _toy_pred(ds), cases=np.array([0]), available={"buff_top"})


def test_run_all_zones(ds):
    out = responsivity.run_all_zones(ds, _toy_pred(ds), set(config.TARGETS), n_cases=15)
    assert set(config.RESP_ZONES) <= set(out) and out["dod_zone"] == "eg"
    assert out["dod_passed"] == out["eg"]["dod_passed"] and out["split"] == "valid"
    t = responsivity.run_all_zones(ds, _toy_pred(ds), set(config.TARGETS), split="test", n_cases=10, zones=("dg",))
    assert t["dod_zone"] == "dg" and t["split"] == "test"
    with pytest.raises(ValueError):
        responsivity.select_cases(ds, split="pre")


def test_responsivity_no_effect_fails(ds):
    def pred(t, h, X):
        return np.zeros(len(X))

    r = responsivity.run(ds, pred, cases=responsivity.select_cases(ds, n_cases=20))
    assert not r.passed and np.isnan(r.corr_primary)
    assert r.as_dict()["pearson_primary"] is None


def test_exploded_relative():
    assert evaluate.exploded({"max_abs_pred": 400.0, "max_abs_y": 100.0})
    assert not evaluate.exploded({"max_abs_pred": 300.0, "max_abs_y": 290.0})
    assert not evaluate.exploded({"max_abs_pred": float("nan"), "max_abs_y": 1.0})
    m = _fake_metrics(0.15, 1.2)
    m["flue@4"] = {"valid": {"mae": 5.0, "mae_b0": 6.0, "skill_b0": 0.1, "max_abs_pred": 900.0, "max_abs_y": 300.0}}
    d = evaluate.dod_check({"x": m}, "x")
    assert not d["no_explosion"] and d["exploded"] == ["flue@4"]
