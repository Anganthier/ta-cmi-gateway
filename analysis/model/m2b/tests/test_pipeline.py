"""Tests: End-to-End (Mini-Lauf) → Artefakt → Laden → Vorhersage → CLI → Report."""
from __future__ import annotations

import json

import numpy as np
import pytest

from analysis.model.m2b import __main__ as cli
from analysis.model.m2b import config, keras_model, lgbm
from analysis.model.m2b.pipeline import _jsonable, run_train
from analysis.model.m2b.predict import L1Predictor, format_table

TGTS = ("ras_eg_ist", "buff_top", "pump_hz_on", "hzrkl_eg_on")
HS = (1, 4, 6)


@pytest.fixture(scope="module")
def trained(store, tmp_path_factory):
    out = tmp_path_factory.mktemp("v002")
    rep = out / "REPORT.md"
    s = run_train(
        features_dir=store, out_dir=out, targets=TGTS, horizons=HS, threads=2,
        num_boost=40, keras_epochs=2, resp_cases=25, report_path=rep, log=lambda m: None,
    )
    return out, s, rep


def test_artifact_files(trained):
    out, s, rep = trained
    assert (out / "metadata.json").exists() and (out / "features.json").exists()
    assert (out / "summary.json").exists() and (out / "keras" / "model.keras").exists()
    assert len(list((out / "lgbm").glob("*.txt"))) == len(TGTS) * len(HS)
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["winner"] in ("lgbm", "keras")
    assert meta["deploy_lgbm"] == "refit_train_valid"
    assert set(meta["lgbm_monotone"]) == {f"{t}@{h}" for t in TGTS for h in HS}
    assert meta["data_version"] == config.EXPECTED_DATA_VERSION
    txt = rep.read_text()
    assert "DoD" in txt and "Responsivität" in txt and "v001" in txt


def test_summary_contents(trained):
    _, s, _ = trained
    assert set(s["metrics"]) >= {"lgbm", "lgbm_plain", "keras", "lgbm_refit"}
    r = s["metrics"]["lgbm"]["buff_top@4"]["valid"]
    assert r["n"] > 100 and np.isfinite(r["mae"]) and np.isfinite(r["mae_b0"])
    assert s["dod"]["lgbm"]["no_explosion"]
    assert {"lgbm", "lgbm_plain", "keras", "lgbm_refit@test", "lgbm@test"} <= set(s["responsivity"])
    r = s["responsivity"]["lgbm"]
    assert r["dod_zone"] == "eg" and r["eg"]["primary"] == "hzrkl_eg_on"
    assert r["kell"]["primary"] == "pump_hz_on"  # kein Keller-Ventil trainiert → Pumpe
    assert s["responsivity"]["lgbm_refit@test"]["split"] == "test"
    assert s["lgbm_variants"]["pump_hz_on@1"]["valid_mae_mono"] is not None
    assert s["lgbm_variants"]["buff_top@1"]["valid_mae_mono"] is None


def test_predictor_lgbm_and_keras(trained, store):
    out, _, _ = trained
    for be in ("lgbm", "keras"):
        p = L1Predictor.load(out, backend=be)
        res = p.predict_at("2025-02-10 18:00", features_dir=store)
        assert sorted(res) == [0, 1, 4, 6]
        assert set(res[4]) == set(TGTS)
        assert 0.0 <= res[1]["pump_hz_on"] <= 1.0
        assert abs(res[4]["ras_eg_ist"] - res[0]["ras_eg_ist"]) < 10
        assert 0.0 <= res[4]["hzrkl_eg_on"] <= 1.0
        scen = p.predict_at("2025-02-10 18:00", features_dir=store, offset_delta={"eg": 3.4})
        assert set(scen[6]) == set(TGTS)
        assert "jetzt" in format_table(res)
    with pytest.raises(ValueError):
        L1Predictor.load(out, backend="xyz")


def test_predictor_errors(trained, store):
    out, _, _ = trained
    p = L1Predictor.load(out)
    with pytest.raises(KeyError):
        p.predict_at("1999-01-01 00:00", features_dir=store)
    p.feature_names = p.feature_names[:-1]
    with pytest.raises(ValueError):
        p.predict_at("2025-02-10 18:00", features_dir=store)


def test_cli_predict_report_train(trained, store, tmp_path, capsys):
    out, _, _ = trained
    assert cli.main(["predict", "--ts", "2025-02-10 18:00", "--model", str(out),
                     "--features-dir", str(store), "--offset-kell", "2"]) == 0
    assert "Szenario" in capsys.readouterr().out
    assert cli.main(["predict", "--ts", "2025-02-10 18:00", "--model", str(out),
                     "--features-dir", str(store), "--json", "--backend", "lgbm"]) == 0
    assert '"4"' in capsys.readouterr().out
    assert cli.main(["report", "--model", str(out), "--out", str(tmp_path / "R.md")]) == 0
    assert (tmp_path / "R.md").exists()
    o2 = tmp_path / "quick"
    assert cli.main(["train", "--features-dir", str(store), "--out", str(o2), "--report", str(o2 / "R.md"),
                     "--targets", "buff_top", "--horizons", "1", "--no-keras", "--no-refit",
                     "--no-monotone", "--num-boost", "20", "--threads", "2"]) == 0
    meta = json.loads((o2 / "metadata.json").read_text())
    assert meta["winner"] == "lgbm" and meta["deploy_lgbm"] == "train_only"


def test_lgbm_helpers(ds):
    v = lgbm.monotone_vector("pump_hz_on", ds.feature_names)
    assert v[ds.feature_names.index("offset_kell")] == -1
    assert v[ds.feature_names.index("ext")] == 0
    assert set(lgbm.monotone_vector("buff_top", ds.feature_names)) == {0}
    import copy

    d2 = copy.copy(ds)
    d2.split = np.full(ds.n, "test", dtype=object)  # keine Train-/Valid-Zeilen
    d2._cache = {}
    with pytest.raises(ValueError):
        lgbm.fit_one(d2, "buff_top", 1, num_boost=5)


def test_mask_excluded_and_predict_consistent(trained, ds):
    out, _, _ = trained
    X = ds.X[:20]
    Xm = lgbm.mask_excluded("pump_hz_on", X, ds.feature_names)
    j = ds.feature_names.index("ras_kell_ist")
    assert np.isnan(Xm[:, j]).all() and not np.isnan(X[:, j]).all()
    assert lgbm.mask_excluded("buff_top", X, ds.feature_names) is X
    assert lgbm.mask_excluded("hzrkl_eg_on", X, ds.feature_names) is not X
    m = lgbm.LgbmL1.load(out / "lgbm", [("pump_hz_on", 4)])
    X2 = X.copy()
    X2[:, j] += 5.0  # ausgeblendetes Feature darf die Vorhersage nicht ändern
    np.testing.assert_allclose(m.predict("pump_hz_on", 4, X), m.predict("pump_hz_on", 4, X2))


def test_digital_rmse_metric(trained):
    _, s, _ = trained
    r = s["metrics"]["lgbm"]["pump_hz_on@4"]["valid"]
    assert {"rmse", "rmse_b0", "skill_b0_rmse"} <= set(r)
    assert s["config"]["excluded_features"]["pump_hz_on"]
    assert s["split_rows"]["pre"] == 0 and s["train_start"] is None


def test_cli_train_start(store, tmp_path):
    o = tmp_path / "ts"
    assert cli.main(["train", "--features-dir", str(store), "--out", str(o), "--report", str(o / "R.md"),
                     "--targets", "buff_top", "--horizons", "1", "--no-keras", "--no-refit", "--no-monotone",
                     "--num-boost", "10", "--threads", "2", "--train-start", "2024-11-01 00:00"]) == 0
    meta = json.loads((o / "metadata.json").read_text())
    assert meta["train_start"] == "2024-11-01 00:00" and meta["split_rows"]["pre"] == 31 * 24


def test_masked_huber_ignores_nan():
    import tensorflow as tf

    loss = keras_model.masked_huber(1.0)
    yt = tf.constant([[0.0, np.nan], [2.0, np.nan]], dtype=tf.float32)
    yp = tf.constant([[0.5, 100.0], [0.0, -100.0]], dtype=tf.float32)
    # Elemente: 0.5 → 0.125 (quadratisch), 2.0 → 1.5 (linear); Mittel = 0.8125
    assert float(loss(yt, yp)) == pytest.approx(0.8125, rel=1e-5)
    allnan = tf.constant([[np.nan]], dtype=tf.float32)
    assert float(loss(allnan, tf.constant([[1.0]]))) == pytest.approx(0.0)


def test_keras_prep_roundtrip(trained):
    out, _, _ = trained
    k = keras_model.KerasL1.load(out / "keras")
    d = k.prep.to_dict()
    p2 = keras_model.KerasPrep.from_dict(json.loads(json.dumps(d)))
    assert p2.out_keys == k.prep.out_keys
    np.testing.assert_allclose(p2.out_std, k.prep.out_std)


def test_jsonable():
    o = _jsonable({"a": np.float32(1.5), "b": float("nan"), "c": (np.int64(2), np.bool_(True))})
    assert o == {"a": 1.5, "b": None, "c": [2, True]}


def test_controller_causal_mode(trained, ds, monkeypatch):
    _, s, _ = trained
    assert s["config"]["controller_causal"] is True
    assert all(s["lgbm_variants"][f"{t}@{h}"]["monotone"] for h in HS for t in ("pump_hz_on", "hzrkl_eg_on"))
    p = lgbm._params("pump_hz_on", False, np.zeros(3), ds.feature_names, 2, 1)
    assert p["metric"] == "l1"
    assert lgbm._params("ww_on" if "ww_on" in config.TARGET_KIND else "oel_burner_on", False,
                        np.zeros(3), ds.feature_names, 2, 1)["metric"] == "l2"
    monkeypatch.setattr(config, "CONTROLLER_CAUSAL", False)
    assert lgbm._params("pump_hz_on", False, np.zeros(3), ds.feature_names, 2, 1)["metric"] == "l2"
    assert lgbm._params("buff_top", True, np.ones(3), ds.feature_names, 2, 1)["objective"] == "huber"


def test_config_monotone_spec():
    assert config.monotone_spec("ras_eg_ist") == {"offset_eg": -1}
    assert config.monotone_spec("ww") == {}
    assert config.lgbm_params("digital")["objective"] == "cross_entropy"
