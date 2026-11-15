"""Test: CLI-Hülle von ``m2b.verify_p0`` (der eigentliche Lauf braucht v001 + m1.0-Store)."""
from __future__ import annotations

import json

from analysis.model.m2b import verify_p0


def test_main_writes_json(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(features_dir, art_dir):
        seen["args"] = (features_dir, art_dir)
        return {"splits": {"test": {"max_abs_pred_old": 41710.0, "max_abs_pred_new": 106.0}}}

    monkeypatch.setattr(verify_p0, "run", fake_run)
    out = tmp_path / "p0.json"
    assert verify_p0.main(["--features-dir", str(tmp_path), "--model", str(tmp_path), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["splits"]["test"]["max_abs_pred_new"] == 106.0
    assert seen["args"] == (tmp_path, tmp_path)
    assert "41710" in capsys.readouterr().out
