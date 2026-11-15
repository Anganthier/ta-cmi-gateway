"""Tests: GPU-Enablement (M2-GPU) — Re-Exec-Logik (os.execve gefaket),
configure_devices (CPU-fallback + GPU via monkeypatch), Force-Flag-Matrix
(weiche Weiterleitung train_keras/train_lgbm/compute_metrics) und
LGBM-device-Parameter (Params-Check, kein echtes GPU-Training).

Alle Tests sind GPU-unabhängig (CPU-fall sicher): die nvidia-Existenz wird
per tmp-Dummy site-packages simuliert, ``os.execve`` wird immer gefaked.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pytest

from analysis.model.m2 import build, config, lgbm_model

from .conftest import make_mini_keras, make_mini_lgbm

# --- Re-Exec-Logik (build._nvidia_lib_dirs / _gpu_reexec) -------------------


@pytest.fixture
def fake_site_packages(tmp_path, monkeypatch):
    """Simuliertes site-packages mit nvidia-Bundles (cublas + cudnn, davon
    eines ohne lib/-Verzeichnis → Filter-Test)."""
    sp = tmp_path / "site-packages"
    for name in ("cublas", "cudnn", "kein_lib"):
        d = sp / "nvidia" / name
        d.mkdir(parents=True)
        (d / "lib").mkdir()
    (sp / "nvidia" / "kein_lib" / "lib").rmdir()
    import sysconfig

    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(sp)})
    return sp


def test_nvidia_lib_dirs_present(fake_site_packages):
    dirs = build._nvidia_lib_dirs()
    assert [d.name for d in dirs] == ["cublas", "cudnn"]  # sortiert, lib-Filter
    assert all((d / "lib").is_dir() for d in dirs)


def test_nvidia_lib_dirs_absent(tmp_path, monkeypatch):
    import sysconfig

    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    assert build._nvidia_lib_dirs() == []


def test_nvidia_lib_dirs_unresolvable(monkeypatch):
    monkeypatch.setattr(build, "_site_packages", lambda: None)
    assert build._nvidia_lib_dirs() == []


def _reexec_argv() -> list[str]:
    """Simulierter sys.argv[1:] eines ``python -m analysis.model.m2 build …``
    -Starts: bei -m ist argv[0] der __main__.py-Pfad → argv[1:] = exakt die
    CLI-Argumente (cmd + Flags), wie es der Re-Exec-argv übergeben wird."""
    return ["build", "--force-keras", "--quiet"]


def test_reexec_env_and_argv(fake_site_packages, monkeypatch, capsys):
    """Re-Exec wird ausgelöst: argv = [sys.executable, -m, analysis.model.m2,
    build, --force-keras, --quiet] (CLI-Argumente intakt) + Env-Flags
    CMI_M2_GPU_REEXEC=1 und LD_LIBRARY_PATH mit allen nvidia-Lib-Dirs."""
    monkeypatch.setattr(build, "_QUIET", False)  # Flush-Logzeile sichtbar machen
    monkeypatch.delenv("CMI_M2_GPU_REEXEC", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    calls: list[dict] = []

    def _fake_execve(exec_path, argv, env):
        calls.append({"exec_path": exec_path, "argv": list(argv), "env": dict(env)})

    monkeypatch.setattr(build.os, "execve", _fake_execve)
    assert build._gpu_reexec(_reexec_argv()) is False  # "erfolgreich" gefaked
    assert len(calls) == 1
    c = calls[0]
    assert c["exec_path"] == sys.executable
    assert c["argv"] == [sys.executable, "-m", "analysis.model.m2", "build",
                         "--force-keras", "--quiet"]
    assert c["env"]["CMI_M2_GPU_REEXEC"] == "1"
    ld = c["env"]["LD_LIBRARY_PATH"]
    for name in ("cublas", "cudnn"):
        assert f"/nvidia/{name}/lib" in ld
    assert "/nvidia/kein_lib/lib" not in ld  # nur existierende lib-Dirs
    # Flush-Logzeile vor dem Re-Exec (Prozess wird ersetzt).
    assert "GPU: starte neu mit LD_LIBRARY_PATH" in capsys.readouterr().out


def test_reexec_no_op_when_flag_set(fake_site_packages, monkeypatch):
    """Env gesetzt (zweiter Aufruf / Tests) → kein Re-Exec, kein execve."""
    monkeypatch.setenv("CMI_M2_GPU_REEXEC", "1")
    calls: list = []
    monkeypatch.setattr(build.os, "execve", lambda *a, **k: calls.append(1))
    assert build._gpu_reexec(_reexec_argv()) is False
    assert calls == []


def test_reexec_no_op_without_nvidia(tmp_path, monkeypatch):
    """Kein nvidia-Verzeichnis im site-packages → CPU-fallback (kein Re-Exec)."""
    import sysconfig

    monkeypatch.delenv("CMI_M2_GPU_REEXEC", raising=False)
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    calls: list = []
    monkeypatch.setattr(build.os, "execve", lambda *a, **k: calls.append(1))
    assert build._gpu_reexec(_reexec_argv()) is False
    assert calls == []


def test_main_argv_passthrough_reexec(fake_site_packages, monkeypatch):
    """main() übergibt sys.argv[1:] unverändert (CLI-Argumente: cmd + Flags)
    an den Re-Exec — argv-Konstruktion korrekt (``-m analysis.model.m2``-
    Startform: argv[0] = __main__.py-Pfad, argv[1:] = Argumente).
    cmd = „metrics“ (geparst, aber unknown) → kein echtes build() danach."""
    monkeypatch.delenv("CMI_M2_GPU_REEXEC", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["/…/analysis/model/m2/__main__.py", "metrics", "--force-lgbm", "--lgbm-gpu"],
    )
    calls: list[dict] = []
    monkeypatch.setattr(build.os, "execve",
                        lambda ep, argv, env: calls.append({"argv": list(argv)}))
    with pytest.raises(SystemExit) as ei:
        build.main()
    assert "Unknown command" in str(ei.value)
    assert calls[0]["argv"] == [sys.executable, "-m", "analysis.model.m2",
                                "metrics", "--force-lgbm", "--lgbm-gpu"]


def test_main_reexec_suppressed_in_tests(fake_site_packages, monkeypatch):
    """CMI_M2_GPU_REEXEC=1 (autouse-Test-Fixtur) → main() macht KEINEN
    Re-Exec und parst die CLI-Argumente normal (unknown-cmd-Pfad,
    ohne echtes build())."""
    monkeypatch.setenv("CMI_M2_GPU_REEXEC", "1")
    calls: list = []
    monkeypatch.setattr(build.os, "execve", lambda *a, **k: calls.append(1))
    with pytest.raises(SystemExit) as ei:
        build.main(["metrics"])
    assert "Unknown command" in str(ei.value)
    assert calls == []


# --- configure_devices (model.py) -------------------------------------------


def test_configure_devices_no_gpu(monkeypatch):
    """Keine GPU (CPU-Umgebung) → [] + kein Memory-Growth-Call."""
    import tensorflow as tf

    seen: dict = {"growth": []}
    monkeypatch.setattr(tf.config, "list_physical_devices",
                        lambda kind: [] if kind == "GPU" else ["CPU:0"])
    monkeypatch.setattr(tf.config.experimental, "set_memory_growth",
                        lambda gpu, enabled: seen["growth"].append((gpu, enabled)))
    from analysis.model.m2 import model

    assert model.configure_devices() == []
    assert seen["growth"] == []


def test_configure_devices_gpu_memory_growth(monkeypatch):
    """GPU vorhanden → Device-Namen ["GPU:0"] + Memory-Growth(True)."""
    import tensorflow as tf

    class _FakeGpu:
        name = "/device:GPU:0"

    fake_gpu = _FakeGpu()
    seen: list[tuple] = []
    monkeypatch.setattr(tf.config, "list_physical_devices",
                        lambda kind: [fake_gpu] if kind == "GPU" else [])
    monkeypatch.setattr(tf.config.experimental, "set_memory_growth",
                        lambda gpu, enabled: seen.append((gpu, enabled)))
    from analysis.model.m2 import model

    out = model.configure_devices()
    assert out == ["GPU:0"]
    assert seen == [(fake_gpu, True)]


def test_configure_devices_gpu_growth_error_ignored(monkeypatch):
    """set_memory_growth-Error (Device bereits konfiguriert) → wird nicht
    propagiert; Device wird dennoch gelistet."""
    import tensorflow as tf

    class _FakeGpu:
        name = "/device:GPU:0"

    def _raise(gpu, enabled):
        raise RuntimeError("device already configured")

    monkeypatch.setattr(tf.config, "list_physical_devices",
                        lambda kind: [_FakeGpu()] if kind == "GPU" else [])
    monkeypatch.setattr(tf.config.experimental, "set_memory_growth", _raise)
    from analysis.model.m2 import model

    assert model.configure_devices() == ["GPU:0"]


# --- Force-Flag-Matrix (build.build) -----------------------------------------


class _Capture:
    """Erst-Trainings-/Metric-Capturer (Force-Weiterleitung + Devices)."""

    def __init__(self, mini_store, fake_metrics):
        self._mini = mini_store
        self._fake_metrics = fake_metrics
        self.calls: list[dict] = []

    def __call__(self, *a, **kw):
        return self.calls

    def train_keras(self, *a, **kw):
        self.calls.append({"fn": "train_keras", "force": kw.get("force"),
                           "devices": kw.get("devices")})
        return make_mini_keras(self._mini[2]["train"].shape[1])

    def train_lgbm(self, *a, **kw):
        self.calls.append({"fn": "train_lgbm", "force": kw.get("force"),
                           "device": kw.get("device")})
        return make_mini_lgbm(
            np.zeros((3, 4), dtype=np.float32),
            np.zeros((3, config.N_TARGETS), dtype=np.float32),
            np.zeros((2, 4), dtype=np.float32),
            np.zeros((2, config.N_TARGETS), dtype=np.float32),
            boost=2,
        )

    def compute_metrics(self, *a, **kw):
        self.calls.append({"fn": "compute_metrics", "force": kw.get("force")})
        return self._fake_metrics()


def _run_build_flag_matrix(monkeypatch, mini_store, tmp_path, fake_metrics,
                           force=False, force_keras=False, force_lgbm=False,
                           lgbm_device="cpu", devices=("GPU:0",)):
    splits, stats, X_ext, _f, scaler = mini_store
    cap = _Capture(mini_store, fake_metrics)
    monkeypatch.setattr(build, "train_keras", cap.train_keras)
    monkeypatch.setattr(build, "train_lgbm", cap.train_lgbm)
    monkeypatch.setattr(build, "compute_metrics", cap.compute_metrics)
    monkeypatch.setattr(build, "build_transitions", lambda *a, **k: (splits, stats))
    monkeypatch.setattr(build, "configure_devices", lambda: list(devices))
    monkeypatch.setattr(build, "_git_rev", lambda: "flagtest")
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "ckpt")
    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifact")
    monkeypatch.setattr(config, "REPORT_PATH", tmp_path / "REPORT.md")
    monkeypatch.setattr(config, "ARTIFACT_READY_FLAG", tmp_path / "artifact_ready.json")
    rc = build.build(force=force, force_keras=force_keras, force_lgbm=force_lgbm,
                     lgbm_device=lgbm_device, quiet=True)
    assert rc == 0
    by_fn = {c["fn"]: c for c in cap.calls}
    assert set(by_fn) == {"train_keras", "train_lgbm", "compute_metrics"}
    return by_fn


def _fake_metrics() -> dict[str, object]:
    from .test_build_smoke import _fake_metrics as _fm

    return _fm()


@pytest.mark.parametrize(
    ("force", "force_keras", "force_lgbm", "exp_k", "exp_l", "exp_m"),
    [
        (False, False, False, False, False, False),
        (True, False, False, True, True, True),
        (False, True, False, True, False, True),
        (False, False, True, False, True, True),
        (True, True, True, True, True, True),
    ],
)
def test_build_force_flag_matrix(monkeypatch, mini_store, tmp_path,
                                 force, force_keras, force_lgbm,
                                 exp_k, exp_l, exp_m):
    """Force-Semantik: train_keras ← force or force_keras; train_lgbm ←
    force or force_lgbm; compute_metrics ← force or force_keras or force_lgbm."""
    calls = _run_build_flag_matrix(
        monkeypatch, mini_store, tmp_path, _fake_metrics,
        force=force, force_keras=force_keras, force_lgbm=force_lgbm,
    )
    assert calls["train_keras"]["force"] is exp_k
    assert calls["train_lgbm"]["force"] is exp_l
    assert calls["compute_metrics"]["force"] is exp_m


def test_build_devices_and_lgbm_device_flow(monkeypatch, mini_store, tmp_path):
    """devices (Keras) → train_keras + metadata.json + report; lgbm_device →
    train_lgbm + metadata.json + report."""
    calls = _run_build_flag_matrix(
        monkeypatch, mini_store, tmp_path, _fake_metrics,
        lgbm_device="gpu", devices=("GPU:0",),
    )
    assert calls["train_keras"]["devices"] == ["GPU:0"]
    assert calls["train_lgbm"]["device"] == "gpu"
    meta = json.loads((config.ARTIFACT_DIR / "metadata.json").read_text())
    assert meta["device"] == ["GPU:0"]
    assert meta["lgbm_device"] == "gpu"
    rep = config.REPORT_PATH.read_text()
    assert "Device: GPU:0" in rep
    assert "LGBM-Device: **gpu**" in rep


def test_build_default_cpu_run_tag(monkeypatch, mini_store, tmp_path):
    """Kein Device (CPU-fallback) → Device: CPU + CPU-Run-Tag im Report;
    metadata device = []."""
    calls = _run_build_flag_matrix(
        monkeypatch, mini_store, tmp_path, _fake_metrics, devices=(),
    )
    assert calls["train_keras"]["devices"] == []
    assert calls["train_lgbm"]["device"] == "cpu"
    meta = json.loads((config.ARTIFACT_DIR / "metadata.json").read_text())
    assert meta["device"] == []
    assert meta["lgbm_device"] == "cpu"
    rep = config.REPORT_PATH.read_text()
    assert "Device: CPU" in rep
    assert "(CPU-Run, deterministisch)" in rep


# --- train_keras / train_lgbm Meta (device-Fields) ---------------------------


def test_train_keras_meta_device_field(tmp_path, monkeypatch, mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    monkeypatch.setattr(config, "EPOCHS_DEFAULT", 2)
    monkeypatch.setattr(config, "PATIENCE", 1)
    build.train_keras(X_ext["train"], splits["train"].Y, X_ext["valid"],
                      splits["valid"].Y, scaler, force=False,
                      devices=["GPU:0"])
    meta = json.loads((ck / "keras_train_meta.json").read_text())
    assert meta["device"] == ["GPU:0"]
    assert meta["seed"] == config.SEED


def test_train_keras_meta_device_default_cpu(tmp_path, monkeypatch, mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    monkeypatch.setattr(config, "EPOCHS_DEFAULT", 2)
    monkeypatch.setattr(config, "PATIENCE", 1)
    build.train_keras(X_ext["train"], splits["train"].Y, X_ext["valid"],
                      splits["valid"].Y, scaler, force=False)
    meta = json.loads((ck / "keras_train_meta.json").read_text())
    assert meta["device"] == []  # None → CPU


def _te_fast_patch(monkeypatch, num_boost: int = 2):
    """train_ensemble auf wenige Boost-Runden beschleunigen (Meta-/Flag-Tests
    prüfen nur Parameter-Flow, nicht das Training selbst)."""
    import analysis.model.m2.build as build_mod

    real_te = build_mod.train_ensemble

    def _te_fast(*a, **kw):
        kw["num_boost_round"] = num_boost
        kw["early_stopping_rounds"] = num_boost
        return real_te(*a, **kw)

    monkeypatch.setattr(build_mod, "train_ensemble", _te_fast)


def test_train_lgbm_meta_device_and_version(tmp_path, monkeypatch, mini_store):
    import lightgbm as lgb

    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    _te_fast_patch(monkeypatch)
    build.train_lgbm(
        X_ext["train"][:40], splits["train"].Y[:40],
        X_ext["valid"][:20], splits["valid"].Y[:20], scaler,
        force=True, device="gpu",
    )
    meta = json.loads((ck / "lgbm_train_meta.json").read_text())
    assert meta["device"] == "gpu"
    assert meta["lightgbm"] == str(lgb.__version__)
    assert meta["n_train"] == 40


def test_train_lgbm_device_cpu_normalized(tmp_path, monkeypatch, mini_store):
    """device='cpu' (Default) → meta 'cpu' (bit-reproduzierbarer Pfad)."""
    splits, _s, X_ext, _f, scaler = mini_store
    ck = tmp_path / "ck"
    monkeypatch.setattr(config, "CHECKPOINT_DIR", ck)
    _te_fast_patch(monkeypatch)
    build.train_lgbm(
        X_ext["train"][:40], splits["train"].Y[:40],
        X_ext["valid"][:20], splits["valid"].Y[:20], scaler,
        force=True, device="cpu",
    )
    meta = json.loads((ck / "lgbm_train_meta.json").read_text())
    assert meta["device"] == "cpu"


# --- LGBM-Device-Parameter (Params-Check, kein echtes GPU-Training) ----------


def test_lgbm_params_device_cpu_default():
    p = lgbm_model.lgbm_params()
    assert p["device"] == "cpu"
    assert p["deterministic"] is True
    assert p["force_row_wise"] is True  # CPU-Default (bit-reproduzierbar)


def test_lgbm_params_device_gpu():
    p = lgbm_model.lgbm_params(device="gpu")
    assert p["device"] == "gpu"
    assert p["deterministic"] is True  # bleibt gesetzt (dokumentiert: keine
    # Bit-Reproduzierbarkeits-Garantie auf GPU → Default CPU)
    assert "force_row_wise" not in p  # CPU-only-Flag entfällt


def test_train_ensemble_device_param_in_ensemble(mini_store):
    splits, _s, X_ext, _f, scaler = mini_store
    ens, _ev = lgbm_model.train_ensemble(
        scaler.transform(X_ext["train"][:24]), splits["train"].Y[:24],
        scaler.transform(X_ext["valid"][:12]), splits["valid"].Y[:12],
        num_boost_round=2, early_stopping_rounds=2,
        weights={t: 1.0 for t in config.TARGETS},
        device="cpu",
    )
    assert ens.params["device"] == "cpu"
    assert len(ens.models) == len(config.TARGETS)


# --- Report-Device-Rendering (Unit) ------------------------------------------


def test_report_device_rendering():
    from analysis.model.m2 import report

    from .test_build_smoke import _fake_results

    res = _fake_results()
    res["devices"] = ["GPU:0"]
    res["lgbm_device"] = "cpu"
    text = report.render_report(res)
    assert "Device: GPU:0" in text
    assert "(GPU-Run, deterministischer Seed + TF_CUBLAS_DETERMINISTIC=1)" in text
    assert "(CPU-Only" not in text

    res2 = _fake_results()
    res2["devices"] = []
    res2["lgbm_device"] = "cpu"
    text2 = report.render_report(res2)
    assert "Device: CPU" in text2
    assert "(CPU-Run, deterministisch)" in text2
