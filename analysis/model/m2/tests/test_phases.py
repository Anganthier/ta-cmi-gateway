"""Tests: Build-/Baseline-/Rollout-/Responsivitäts-Phasen (Mini-Modelle,
keine echten Trainings-Läufe) — deckt die Phasen-Funktionen von
``m2.build`` / ``m2.baseline`` / ``m2.rollout`` / ``m2.responsivity`` ab."""
from __future__ import annotations

import json

import numpy as np

from analysis.model.m2 import baseline, build, config, lgbm_model, responsivity, rollout
from analysis.model.m2.normalize import StandardScaler
from analysis.model.m2.transitions import Split

from .conftest import make_mini_keras, make_mini_lgbm

# --- helpers ---------------------------------------------------------------


def _mini_lgbm(mini_store, n_tr: int = 100, n_va: int = 40, boost: int = 3):
    splits, _stats, X_ext, _flags, scaler = mini_store
    return make_mini_lgbm(
        scaler.transform(X_ext["train"][:n_tr]),
        splits["train"].Y[:n_tr],
        scaler.transform(X_ext["valid"][:n_va]),
        splits["valid"].Y[:n_va],
        boost=boost,
    )


def _mk_split(n=48, start=None):
    """Lückenloses Stunden-Raster, bekannte Y (wie test_baseline._mini_split)."""
    from datetime import datetime, timedelta

    start = start or datetime(2025, 2, 1)
    ts = [start + timedelta(hours=i) for i in range(n)]
    rng = np.random.default_rng(0)
    Y = rng.normal(20.0, 1.0, size=(n, config.N_TARGETS)).astype(np.float32)
    X = rng.normal(0.0, 1.0, size=(n, 4)).astype(np.float32)
    return Split(
        name="valid", X=X, Y=Y, ts=ts,
        feature_null_mask=np.zeros((n, 4), dtype="uint8"),
        target_null_mask=np.zeros((n, config.N_TARGETS), dtype="uint8"),
    )


# --- build.prepare_data ----------------------------------------------------


def test_prepare_data_fresh_then_cache(tmp_path, monkeypatch, mini_store):
    splits, stats, _X, _f, _sc = mini_store
    monkeypatch.setattr(build, "build_transitions", lambda *a, **k: (splits, stats))
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ck")
    s2, extra = build.prepare_data(force=False)
    assert s2 is splits
    assert set(extra.keys()) == {"stats", "X_ext", "flag_names", "split_info"}
    n_flags = len(config.FEATURE_INPUTS) + len(config.ANALOG_TARGETS)
    assert len(extra["flag_names"]) == n_flags
    for nm in ("train", "valid", "test"):
        assert extra["X_ext"][nm].shape[1] == config.N_FEATURES + n_flags
    assert (tmp_path / "ck" / "scaler.json").exists()
    assert (tmp_path / "ck" / "transitions_stats.json").exists()
    assert extra["split_info"]["n_flag_cols"] == n_flags
    # Zweiter Aufruf: Caches (Transitions-Stats + Scaler) werden geladen.
    out2 = build.prepare_data(force=False)
    sc2 = StandardScaler.load_json(tmp_path / "ck" / "scaler.json")
    assert sc2.n_features == config.N_FEATURES + n_flags
    _ = out2


def test_prepare_data_force_refits(tmp_path, monkeypatch, mini_store):
    splits, stats, _X, _f, _sc = mini_store
    monkeypatch.setattr(build, "build_transitions", lambda *a, **k: (splits, stats))
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ck")
    build.prepare_data(force=False)
    extra = build.prepare_data(force=True)[1]
    # Force: Scaler wird neu gefittet (Cache ignoriert) — Datei bleibt gültig.
    sc = StandardScaler.load_json(tmp_path / "ck" / "scaler.json")
    assert sc.n_features == extra["X_ext"]["train"].shape[1]


def test_stats_dict_keys(mini_store):
    _splits, stats, *_ = mini_store
    d = build._stats_dict(stats)
    assert set(d) == {
        "n_rows_total", "n_rows_gap_full", "n_drops_gap_current",
        "n_drops_missing_next", "n_drops_gap_full_next", "n_transitions",
        "n_imputed_cells", "split_sizes",
    }


# --- build.train_keras / train_lgbm (Cache + Mini-Neutraining) -------------


def test_train_keras_cache_branch(tmp_path, monkeypatch, mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    n_ext = X_ext["train"].shape[1]
    m = make_mini_keras(n_ext)
    ck = tmp_path / "ck"
    ck.mkdir(parents=True)
    m.save(str(ck / "keras_l1.keras"))
    (ck / "keras_train_meta.json").write_text(
        json.dumps({"n_train": int(splits["train"].Y.shape[0]), "n_features": int(n_ext)})
    )
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    out = build.train_keras(X_ext["train"], splits["train"].Y, X_ext["valid"],
                            splits["valid"].Y, scaler, force=False)
    p = build.predict_targets(out, scaler.transform(X_ext["valid"][:16]), batch_size=16)
    assert p.shape == (16, len(config.TARGETS))


def test_train_keras_fresh_minimal(tmp_path, monkeypatch, mini_store):
    """Neutraining-Path (2 Epochs, Patience 1) mit voll. Architektur:
    build_model + assert_param_budget + Callbacks + Meta-Save."""
    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    monkeypatch.setattr(config, "EPOCHS_DEFAULT", 2)
    monkeypatch.setattr(config, "PATIENCE", 1)
    m = build.train_keras(X_ext["train"], splits["train"].Y, X_ext["valid"],
                          splits["valid"].Y, scaler, force=False)
    assert build.count_params(m) < config.MAX_PARAMS
    meta = json.loads((ck / "keras_train_meta.json").read_text())
    assert meta["n_train"] == int(splits["train"].Y.shape[0])
    assert meta["n_features"] == X_ext["train"].shape[1]
    assert meta["epochs_run"] <= 2
    assert (ck / "keras_l1.keras").exists()


def test_train_lgbm_cache_branch(tmp_path, monkeypatch, mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    ck.mkdir(parents=True)
    ens = _mini_lgbm(mini_store, n_tr=100, n_va=40)
    ens.save(ck / "lgbm_l1")
    (ck / "lgbm_train_meta.json").write_text(
        json.dumps({"n_train": int(splits["train"].Y.shape[0])})
    )
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    out = build.train_lgbm(X_ext["train"], splits["train"].Y, X_ext["valid"],
                           splits["valid"].Y, scaler, force=False)
    p = out.predict_targets(scaler.transform(X_ext["valid"][:16]))
    assert p.shape == (16, len(config.TARGETS))


def test_train_lgbm_fresh_minimal(tmp_path, mini_store):
    """lgbm_model.train_ensemble (frisch) + Save/Load-Roundtrip + evaluate."""
    splits, _s, X_ext, _f, scaler = mini_store
    out, evals = lgbm_model.train_ensemble(
        scaler.transform(X_ext["train"][:60]), splits["train"].Y[:60],
        scaler.transform(X_ext["valid"][:40]), splits["valid"].Y[:40],
        num_boost_round=3, early_stopping_rounds=2,
        weights={t: 1.0 for t in config.TARGETS},
    )
    assert set(evals) == set(config.TARGETS)
    for v in evals.values():
        assert "best_iteration" in v and "best_valid_l1" in v
    paths = out.save(tmp_path / "lgbm_l1")
    assert len(paths) == len(config.TARGETS)
    ens2 = lgbm_model.LGBMEnsemble.load(tmp_path / "lgbm_l1")
    X = scaler.transform(X_ext["valid"][:8])
    np.testing.assert_allclose(out.predict_targets(X), ens2.predict_targets(X),
                               atol=1e-6, rtol=1e-6)
    mae = lgbm_model.evaluate(ens2, X, splits["valid"].Y[:8])
    assert set(mae) == set(config.TARGETS) | {"weighted_mean"}
    # predict_raw-Alias (Rollout-pred_fn).
    np.testing.assert_allclose(out.predict_raw(X), out.predict_targets(X))


# --- build._mae_1h_valid / _rollout_mae_valid / _worst_day_keras -----------


def test_mae_1h_valid_b2(mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    m = build._mae_1h_valid("b2", splits, X_ext, None, scaler)
    assert set(m) == set(config.TARGETS)
    assert all(np.isfinite(v) for v in m.values())


def test_mae_1h_valid_keras(mini_store, mini_keras):
    splits, _s, X_ext, _f, scaler = mini_store
    m = build._mae_1h_valid("keras", splits, X_ext, mini_keras, scaler)
    assert set(m) == set(config.TARGETS)


def test_mae_1h_valid_lgbm(mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    m = build._mae_1h_valid("lgbm", splits, X_ext, _mini_lgbm(mini_store), scaler)
    assert set(m) == set(config.TARGETS)
    assert all(np.isfinite(v) for v in m.values())


def test_rollout_mae_valid_keras(mini_store, mini_keras):
    splits, _s, _X, _f, scaler = mini_store
    m = build._rollout_mae_valid("keras", splits, mini_keras, scaler)
    assert set(m) == set(config.TARGETS)
    assert all(np.isfinite(v) for v in m.values())


def test_rollout_mae_valid_lgbm_b2(mini_store):
    splits, _s, _X, _f, scaler = mini_store
    m_l = build._rollout_mae_valid("lgbm", splits, _mini_lgbm(mini_store), scaler)
    m_b = build._rollout_mae_valid("b2", splits, None, scaler)
    for m in (m_l, m_b):
        assert set(m) == set(config.TARGETS)
        assert all(np.isfinite(v) for v in m.values())


def test_worst_day_keras(mini_store, mini_keras):
    splits, _s, X_ext, _f, scaler = mini_store
    d = build._worst_day_keras(splits, X_ext, mini_keras, scaler)
    assert "date" in d
    for c in config.RAS_IST_TARGETS:
        assert c in d and np.isfinite(d[c])
    # Kein Test-Split → leeres Dict.
    no_test = {"train": splits["train"], "valid": splits["valid"]}
    assert build._worst_day_keras(no_test, X_ext, mini_keras, scaler) == {}


def test_git_rev():
    rev = build._git_rev()
    assert isinstance(rev, str) and len(rev) >= 4


# --- build.compute_metrics -------------------------------------------------


def _fake_resp_summary() -> dict:
    return {
        "n_cases": 10, "n_runs": 40, "deltas_k": list(config.RESP_DELTAS_K),
        "pearson_delta_offset_dH": -0.7, "pearson_delta_offset_dD": -0.3,
        "dH_mittel_je_delta": {"+1.0K": -0.1, "+2.0K": -0.2, "+3.4K": -0.3, "+5.0K": -0.4},
        "dD_mittel_je_delta": {"+3.4K": -0.05},
        "dH_neg_anteil_3.4K": 0.8, "dod_corr_ok": True, "dod_frac_ok": True,
        "dod_passed": True,
    }


class _FakeResp:
    def summary(self):
        return _fake_resp_summary()


def test_compute_metrics_full(tmp_path, monkeypatch, mini_store, mini_keras):
    splits, _s, X_ext, _f, scaler = mini_store
    ens = _mini_lgbm(mini_store)
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ck")
    monkeypatch.setattr(build, "_mae_1h_valid",
                        lambda kind, *a, **k: {t: 0.5 for t in config.TARGETS})
    monkeypatch.setattr(build, "_rollout_mae_valid",
                        lambda kind, *a, **k: {t: 0.6 for t in config.TARGETS})
    monkeypatch.setattr(responsivity, "run_responsivity_keras",
                        lambda *a, **k: (_FakeResp(), np.array([0, 1])))
    monkeypatch.setattr(responsivity, "run_responsivity_lgbm",
                        lambda *a, **k: (_FakeResp(), np.array([0, 1])))
    monkeypatch.setattr(build, "_worst_day_keras",
                        lambda *a, **k: {"date": "2026-01-02",
                                         **{c: 0.9 for c in config.RAS_IST_TARGETS}})
    out = build.compute_metrics(splits, X_ext, mini_keras, ens, scaler, force=False)
    for key in ("maes_keras_valid_1h", "maes_b2_valid_1h", "maes_lgbm_valid_1h",
                "maes_keras_valid_4h", "maes_b2_valid_4h", "maes_lgbm_valid_4h",
                "maes_keras_test_4h", "maes_lgbm_test_4h", "maes_b2_test_4h"):
        assert set(out[key]) == set(config.TARGETS)
    assert out["responsivity_keras"]["dod_passed"] is True
    assert out["test_2026"]["worst_day"]["date"] == "2026-01-02"
    assert (tmp_path / "ck" / "metrics.json").exists()
    # Cache: zweiter Aufruf liefert identisches Dict ohne Neuberechnung.
    out2 = build.compute_metrics(splits, X_ext, mini_keras, ens, scaler, force=False)
    assert out2["maes_keras_valid_1h"] == out["maes_keras_valid_1h"]


def test_compute_metrics_no_test_split(tmp_path, monkeypatch, mini_store, mini_keras):
    splits, _s, X_ext, _f, scaler = mini_store
    no_test = {"train": splits["train"], "valid": splits["valid"]}
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ck")
    monkeypatch.setattr(build, "_mae_1h_valid",
                        lambda kind, *a, **k: {t: 0.5 for t in config.TARGETS})
    monkeypatch.setattr(build, "_rollout_mae_valid",
                        lambda kind, *a, **k: {t: 0.6 for t in config.TARGETS})
    monkeypatch.setattr(responsivity, "run_responsivity_keras",
                        lambda *a, **k: (_FakeResp(), np.array([0])))
    monkeypatch.setattr(responsivity, "run_responsivity_lgbm",
                        lambda *a, **k: (_FakeResp(), np.array([0])))
    monkeypatch.setattr(build, "_worst_day_keras", lambda *a, **k: {})
    out = build.compute_metrics(no_test, X_ext, mini_keras, _mini_lgbm(mini_store), scaler)
    assert out["maes_keras_test_4h"] == {}
    assert out["test_2026"]["worst_day"] == {}


# --- baseline --------------------------------------------------------------


def test_mae_rollout():
    pred = np.zeros((2, 4, 3))
    truth = np.ones((2, 4, 3))
    out = baseline.mae_rollout(pred, truth)
    assert set(out) == {"step1", "step2", "step3", "step4", "all"}
    assert all(abs(v - 1.0) < 1e-9 for v in out.values())


def test_b2_4h_series_excluded():
    split = _mk_split(n=48)
    # i=0: „gestern" (ts[1]−24h) existiert im Raster nicht → excluded.
    pred, n_excl = baseline.b2_4h_series(split, np.array([0]), steps=4)
    assert n_excl == 1
    assert np.isnan(pred[0]).all()
    # i=23: ts_target = 2025-02-02T00:00; −24h = 2025-02-01T00:00 = ts[0]
    # → j=0 → Y[j−1] ungueltig → excluded (Rand-Schutz, j==0).
    pred2, n_excl2 = baseline.b2_4h_series(split, np.array([23]), steps=4)
    assert n_excl2 == 1
    assert np.isnan(pred2[0]).all()
    # Gültiger Start (i=30): Persistenz Y[29] + gestern Y[j−1] (j≠0).
    pred3, n_excl3 = baseline.b2_4h_series(split, np.array([30]), steps=4)
    assert n_excl3 == 0
    assert not np.isnan(pred3[0]).any()


def test_rollout_series_keras(mini_store, mini_keras):
    splits, _s, _X, _f, scaler = mini_store
    valid = splits["valid"]
    starts = baseline.rollout_start_indices(valid, steps=4)[:6]
    out = baseline.rollout_series_keras(mini_keras, scaler, valid, starts, steps=4)
    assert out.shape == (6, 4, len(config.TARGETS))
    assert np.isfinite(out).all()
    # action_fixed (Offset-Konstante) → läuft ebenfalls.
    out2 = baseline.rollout_series_keras(
        mini_keras, scaler, valid, starts, steps=4,
        action_fixed={"offset_kell": 2.0},
    )
    assert out2.shape == out.shape


def test_rollout_series_lgbm(mini_store):
    splits, _s, _X, _f, scaler = mini_store
    valid = splits["valid"]
    starts = baseline.rollout_start_indices(valid, steps=4)[:6]
    out = baseline.rollout_series_lgbm(_mini_lgbm(mini_store), scaler, valid, starts, steps=4)
    assert out.shape == (6, 4, len(config.TARGETS))
    assert np.isfinite(out).all()


# --- rollout (Single-Case-Dict + Utilities) --------------------------------


def test_raw_from_vector_and_split_row(mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    n_ext = X_ext["train"].shape[1]
    X0s = scaler.transform(X_ext["train"][:1])[0]
    raw = rollout.raw_from_vector(scaler, X0s)
    assert set(raw.keys()) == set(rollout.feature_names(n_ext))
    # Split-Row (Raw-405) → Raw-Dict mit FEATURE_INPUTS-Namen.
    raw2 = rollout.raw_from_split_row(splits["train"], 0)
    assert set(raw2.keys()) == set(config.FEATURE_INPUTS)
    assert raw2["hour"] == float(splits["train"].X[0, config.FEATURE_INPUTS.index("hour")])


def test_apply_rollout_step(mini_store):
    _splits, _s, X_ext, _f, scaler = mini_store
    raw = rollout.raw_from_vector(scaler, scaler.transform(X_ext["train"][:1])[0])
    hour0 = int(round(raw["hour"]))
    dow0 = int(round(raw["day_of_week"]))
    pred = np.tile(np.arange(len(config.TARGETS), dtype=np.float64), (1,))
    rollout.apply_rollout_step(raw, pred, config.TARGETS, action_fixed=None)
    # State: Analog-Targets → Prädikat.
    i_ras = config.TARGETS.index("ras_kell_ist")
    assert abs(raw["ras_kell_ist"] - float(pred[i_ras])) < 1e-9
    i_off = config.TARGETS.index("offset_kell")
    assert abs(raw["offset_kell"] - float(pred[i_off])) < 1e-9
    # Digital: geclippt auf [0,1] (Prädikat > 1 → 1.0).
    i_pump = config.TARGETS.index("pump_ww_on")
    assert raw["pump_ww_on"] == min(float(pred[i_pump]), 1.0)
    # Kalender: Stunde +1 (mod 24), Wochentag nur bei Tageswechsel.
    assert raw["hour"] == float((hour0 + 1) % 24)
    if hour0 == 23:
        assert raw["day_of_week"] == float((dow0 + 1) % 7)
    else:
        assert raw["day_of_week"] == float(dow0)
    # action_fixed: Offset + Digital fixiert.
    raw2 = dict(raw)
    rollout.apply_rollout_step(raw2, pred, config.TARGETS,
                               action_fixed={"offset_kell": 9.0, "pump_ww_on": 0.4})
    assert raw2["offset_kell"] == 9.0
    assert raw2["pump_ww_on"] == 0.4


def test_rollout_dict_steps_and_calendar_rollover(mini_store):
    _splits, _s, X_ext, _f, scaler = mini_store
    raw = rollout.raw_from_vector(scaler, scaler.transform(X_ext["train"][:1])[0])
    raw["hour"] = 23.0
    raw["day_of_week"] = 5.0
    calls = {"n": 0}

    def _pred(r):
        calls["n"] += 1
        return np.zeros(len(config.TARGETS))

    preds = rollout.rollout(raw, _pred, rollout.RolloutConfig(steps=3, targets=config.TARGETS))
    assert len(preds) == 3
    assert calls["n"] == 3  # Steps, nicht Steps+1
    # rollouT() kopiert den State intern (Input-raw bleibt unangetastet);
    # Kalender-Advance per apply_rollout_step direkt prüfen:
    r2 = dict(raw)
    pred = np.zeros(len(config.TARGETS))
    rollout.apply_rollout_step(r2, pred, config.TARGETS)  # 23 → 0, 5 → 6
    assert r2["hour"] == 0.0
    assert r2["day_of_week"] == 6.0
    assert r2["is_weekend"] == 1.0  # dow 6 = Sonntag (Konvention 0=Mo … 6=So)
    rollout.apply_rollout_step(r2, pred, config.TARGETS)  # 0 → 1, kein Wechsel
    assert r2["hour"] == 1.0
    assert r2["day_of_week"] == 6.0


def test_state_feature_indices(mini_store):
    _splits, _s, X_ext, _f, _sc = mini_store
    idx = rollout.state_feature_indices(X_ext["train"].shape[1])
    assert idx["offset_kell"] == config.FEATURE_INPUTS.index("offset_kell")
    assert idx["solar_coll"] == config.FEATURE_INPUTS.index("solar_coll")
    assert idx["hour"] == config.FEATURE_INPUTS.index("hour")
    assert "solar_coll_null" in idx  # Flag-Spalte


def test_rollout_keras_and_lgbm_single(mini_store, mini_keras):
    _splits, _s, X_ext, _f, scaler = mini_store
    X0s = scaler.transform(X_ext["train"][:1])[0]
    dicts = rollout.rollout_keras(mini_keras, scaler, X0s, steps=2)
    assert len(dicts) == 2
    assert set(dicts[0].keys()) == set(config.TARGETS)
    raw0 = rollout.raw_from_vector(scaler, X0s)
    ens = _mini_lgbm(mini_store)
    names = rollout.feature_names(len(raw0))

    def _lgbm_pred(r):
        vec = np.array([[float(r[n]) for n in names]], dtype=np.float32)
        return ens.predict_targets(scaler.transform(vec))[0]

    arrs = rollout.rollout_lgbm(_lgbm_pred, raw0, steps=2)
    assert len(arrs) == 2


# --- responsivity (intern) -------------------------------------------------


def test_fixed_matrix_hold_and_step(mini_store):
    splits, _s, _X, _f, _sc = mini_store
    valid = splits["valid"]
    idx = np.array([0, 1, 2])
    Xr = valid.X[idx]
    names = rollout.feature_names(Xr.shape[1])
    ni = {n: i for i, n in enumerate(names)}
    hold = responsivity._fixed_matrix(Xr, None)
    step = responsivity._fixed_matrix(Xr, 3.4)
    # Hold: alle Digital-Actions + Offsets = t0-Werte, Rest NaN.
    for n in config.DIGITAL_ACTION_INPUTS:
        assert np.allclose(hold[:, ni[n]], Xr[:, ni[n]])
    for n in config.OFFSET_INPUTS:
        assert np.allclose(hold[:, ni[n]], Xr[:, ni[n]])
    assert np.isnan(hold[:, ni["ext"]]).all()
    assert np.isnan(hold[:, ni["wx_t0_t_bias"]]).all()
    # Step: offset_kell = t0 + 3,4, übrige Offsets = t0.
    assert np.allclose(step[:, ni["offset_kell"]], Xr[:, ni["offset_kell"]] + 3.4)
    for n in ("offset_eg", "offset_dg"):
        assert np.allclose(step[:, ni[n]], Xr[:, ni[n]])


def test_run_rollouts_batched(mini_store):
    splits, _s, _X, _f, _sc = mini_store
    valid = splits["valid"]
    targets = config.TARGETS
    idx_map = {n: i for i, n in enumerate(rollout.feature_names(valid.X.shape[1]))}
    oi, ihm = idx_map["offset_kell"], targets.index("heat_min_kell")

    class _Fn:
        def __call__(self, X):
            P = np.zeros((len(X), len(targets)))
            P[:, ihm] = -0.3 * (X[:, oi] - 1.0) + 5.9
            return P

    fn = _Fn()
    case_idx = np.array([0, 1, 2, 3])
    hold_p, step_p = responsivity._run_rollouts(
        lambda fb: (lambda xb: fn(xb)), valid, case_idx,
        (1.0, 3.4), 6, targets, batch_size=2,
    )
    assert hold_p.shape == (4, 6, len(targets))
    assert step_p[3.4].shape == (4, 6, len(targets))
    # Offset-Step (3,4 K) → heat_min-Prädikat sinkt gegenüber Hold.
    assert step_p[3.4][:, :, ihm].sum(axis=1).mean() < hold_p[:, :, ihm].sum(axis=1).mean()


# --- __main__ / save_model -------------------------------------------------


def test_main_module_entrypoint():
    import importlib

    mod = importlib.import_module("analysis.model.m2.__main__")
    assert callable(mod.main)


def test_save_model(mini_keras, tmp_path):
    from analysis.model.m2 import model

    model.save_model(mini_keras, tmp_path / "sub" / "m.keras")
    assert (tmp_path / "sub" / "m.keras").exists()
