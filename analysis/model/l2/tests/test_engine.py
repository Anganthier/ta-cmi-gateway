"""Engine (recommend): Stale-Guard, No-change-by-default, JSON-Schema,
Determinismus, DE-Begründung, Executability, Rollout-Smoke (FakePredictor)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from analysis.model.l2 import config
from analysis.model.l2.recommend import recommend, save_json, write_report
from analysis.model.l2.tests.conftest import FakePredictor

# --- Stale-Guard (Design §3.2 R5: hart) ---


def _store_dir(st: Any) -> Any:
    """Store-Pfad aus der Fixture (``st.store_dir``)."""
    return st.store_dir


def test_stale_store_gap(tmp_path: Path, st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Metrik-Lücke ≥ 2 h (16:00/17:00 fehlen) → stale, nur „beibehalten“."""
    from analysis.model.l2.recommend import base_confidence

    rec = recommend("2026-11-01 17:00", features_dir=_store_dir(st),
                    wx=wx_ok, predictor=FakePredictor(), skill_map=skill_map)
    assert rec.stale is True
    assert any("Metrik-Lücke" in r for r in rec.stale_reasons)
    for e in rec.empfehlungen:
        assert e["changed"] is False
        assert "beibehalten" in e["vorgeschlagen"]
    # Stale-Penalty: Konfidenz = Basis-Skill · CONF_STALE_PENALTY (×0,3)
    # → klar unter der unpenalisierten Basis-Konfidenz.
    assert rec.konfidenz < base_confidence(skill_map)
    assert rec.konfidenz == round(base_confidence(skill_map) * config.CONF_STALE_PENALTY, 3)


def test_stale_wx_run_old(tmp_path: Path, st: Any, wx_old: Any, skill_map: Any) -> None:
    """Wetter-Run > 2 h alt → stale (auch ohne Metrik-Lücke)."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_old,
                    predictor=FakePredictor(), skill_map=skill_map)
    assert rec.stale is True
    assert any("Wetter-Run" in r for r in rec.stale_reasons)
    assert all(e["changed"] is False for e in rec.empfehlungen)


def test_stale_wx_unavailable(tmp_path: Path, st: Any, wx_old: Any) -> None:
    """LIVE-Forecast fehlt (API-Fehler) → Fallback + stale, kein Crash.

    Negative Skills → Basis-Konfidenz = FLOOR; × Stale-Penalty = 0,03 < FLOOR.
    """
    neg_skills = {k: -0.5 for k in config.CONF_WEIGHTS}
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=None,
                    predictor=FakePredictor(), skill_map=neg_skills)
    assert rec.stale is True
    assert any("LIVE-Forecast" in r for r in rec.stale_reasons)
    assert rec.to_json()["kontext"]["wx_fallback"] is True
    assert rec.konfidenz < config.CONF_FLOOR


def test_fresh_not_stale(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """ts-Zeile vorhanden + frisches Wetter → nicht stale, Rollouts laufen."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    assert rec.stale is False
    assert rec.stale_reasons == []
    assert rec.n_candidates >= 1


# --- No-change-by-default + Empfehlung ---


def test_no_change_wins_flat_state(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Alles im Band → Basisaktion gewinnt (keine „KI-futtert-um“-Änderung)."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor("no_change"), skill_map=skill_map)
    assert rec.stale is False
    for e in rec.empfehlungen:
        if e.get("tier") == "A":
            assert e["changed"] is False
            assert e["aktuell"] == e["vorgeschlagen"]


def test_change_kell_when_band_broken(st_hot: Any, wx_ok: Any, skill_map: Any) -> None:
    """Keller zu warm (v0.3-FIX-Semantik: Intent = 20 − off_now = 16,5) →
    Keller-Offset ANHEBEN wird empfohlen (Tier A; RAUF = kühlen, korrekt).

    Mini-Store-Hot: perzipiert 25,3 haltend (FakePredictor), Ist 21,8 → 5,3 K
    über dem Intent. Δ>0 senkt perz (Rückkopplung), Δ<0 (Soll rauf = wärmer)
    bewegt den Raum vom Intent WEG → verliert. Keller-Δ = +0,5 K (minimal
    Gesamtkosten: h=0-Identität 5,3² für alle Δ, h>0-Abweichung 4,8 K vor dem
    Band überwiegt die Änderungs-Strafe) — NICHT Δ<0 (v0.2-Ära hätte
    +2,5 K = wärmer empfohlen = der alte Bug).
    """
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st_hot), wx=wx_ok,
                    predictor=FakePredictor("change_kell"), skill_map=skill_map)
    kell = next(e for e in rec.empfehlungen if e["parameter"].endswith("kell"))
    assert kell["changed"] is True
    assert kell["vorgeschlagen"] == "+4.0 K"  # +3,5 + 0,5 (bestes Δ, minimal Gesamtkosten)
    eg = next(e for e in rec.empfehlungen if e["parameter"].endswith("eg"))
    assert eg["changed"] is False


def test_executability_per_type_advisory(st_full: Any, wx_ok: Any, skill_map: Any) -> None:
    """v0.2: jede Empfehlung trägt discovery-geleiteten advisory-Wert (realer
    INVENTAR.json-Default) — je Typ, nie generisch `pending`; **v0.5-rework
    (2026-11-12)**: O8-Modus-Block = ``advisory-manuell`` (Statuszeile, kein
    ``to_value``/``executability_mix``) — NIE ``cmi-settable`` (keine L2-
    Empfehlung mit CMI-Write-Pfad mehr)."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st_full), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    for e in rec.empfehlungen:
        assert e["executability"] != config.EXECUTABILITY_PENDING, e["parameter"]
        assert e["executability"] != config.EXECUTABILITY_CMI_SETTABLE, e["parameter"]
        assert e.get("cmi_hint")
    # je-Typ: RAS-Offset ×3 = advisory-uvr (Q-107), Heizkurve = advisory-ta-
    # designer (Q-108), WW-Ladefenster = advisory-zeitprogramm, Zirkulation =
    # advisory-uvr, v0.5 Minderer ×3 = advisory-uvr, O8-Statuszeile =
    # advisory-manuell (v0.5-rework).
    by_param = {e["parameter"]: e for e in rec.empfehlungen}
    for z in config.ZONES:
        assert by_param[f"RAS-Offset {z}"]["executability"] == config.EXECUTABILITY_ADVISORY_UVR
        assert by_param[f"Minderer-Feinjustierung {z}"]["executability"] == config.EXECUTABILITY_ADVISORY_UVR
    assert by_param["Heizkurve/VL-Soll"]["executability"] == config.EXECUTABILITY_ADVISORY_TA_DESIGNER
    assert by_param["WW-Ladefenster"]["executability"] == config.EXECUTABILITY_ADVISORY_ZEITPROGRAMM
    assert by_param["Zirkulation"]["executability"] == config.EXECUTABILITY_ADVISORY_UVR
    assert by_param["Pumpe Heizkreis O8 (Modus)"]["executability"] == config.EXECUTABILITY_ADVISORY_MANUELL
    o8 = by_param["Pumpe Heizkreis O8 (Modus)"]
    assert o8["o8"]["adresse"] == config.O8_ADDRESS
    assert o8["o8"]["aktueller_modus"] == "AUTO"
    assert "to_value" not in o8
    assert "executability_mix" not in o8
    # Discovery-Bezug im Hint: WW ↔ CMI-Schalter „WW vom Oel“ (writeable=true)
    # → cmi-setzbar, aber (bewusst) nicht empfohlen.
    assert "WW vom Oel" in by_param["WW-Ladefenster"]["cmi_hint"]
    assert "cmi-setzbar" in by_param["WW-Ladefenster"]["cmi_hint"]


def test_de_begruendung_nicht_leer(st_hot: Any, wx_ok: Any, skill_map: Any) -> None:
    """DE-Begründung: nicht leer, enthält Zahlen (Ist-Werte), keine Platzhalter."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st_hot), wx=wx_ok,
                    predictor=FakePredictor("change_kell"), skill_map=skill_map)
    for e in rec.empfehlungen:
        assert len(e["begruendung_de"]) > 40
        assert "{" not in e["begruendung_de"] and "None" not in e["begruendung_de"]
    assert any("°C" in e["begruendung_de"] for e in rec.empfehlungen)


def test_konfidenz_bounds(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Konfidenz innerhalb [FLOOR, CAP] (Stale-Penalty kann unter FLOOR sinken)."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    assert config.CONF_FLOOR <= rec.konfidenz <= config.CONF_CAP
    for e in rec.empfehlungen:
        assert 0.0 < e["konfidenz"] <= config.CONF_CAP


def test_konfidenz_from_skill(st: Any, wx_ok: Any) -> None:
    """Basis-Konfidenz = gewichteter Skill (Klemmung auf [FLOOR, CAP])."""
    from analysis.model.l2.recommend import base_confidence

    assert base_confidence({}) == config.CONF_FLOOR  # keine Skills → Floor
    conf2 = base_confidence(
        {
            ("ras_kell_perc", 12): 0.8,
            ("ras_eg_perc", 12): 0.8,
            ("ras_dg_perc", 12): 0.8,
            ("oel_burner_on", 12): 0.8,
            ("pump_hz_on", 12): 0.8,
        }
    )
    assert conf2 == 0.8  # gewichtet = 0,8 (innerhalb [FLOOR, CAP])
    conf_neg = base_confidence({("ras_kell_perc", 12): -0.5, ("ras_eg_perc", 12): -0.5})
    assert conf_neg == config.CONF_FLOOR  # negative Skills → Floor


# --- JSON-Schema (Design §3.2 + L2 v0.1-Felder) ---


def test_json_schema(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Jedes Output-JSON: model_version, datenstand, wx_quelle/wx_run, stale."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    j = rec.to_json()
    assert j["model_version"] == config.model_version()
    assert j["model_version"].startswith("cmi-brain-v003")
    assert j["horizon_h"] == 12
    assert j["stale"] is False
    assert isinstance(j["empfehlungen"], list) and j["empfehlungen"]
    for e in j["empfehlungen"]:
        for key in ("parameter", "aktuell", "vorgeschlagen", "wirkung_prognose",
                    "begruendung_de", "konfidenz", "executability", "cmi_hint"):
            assert key in e, key
        assert e["zone"] in (None, "kell", "eg", "dg")
        assert e["tier"] in ("A", "B")
    k = j["kontext"]
    for key in ("datenstand", "state_ts", "data_version", "wx_quelle", "wx_run", "stale"):
        assert key in k, key
    assert k["data_version"] == "m1.3"
    json.dumps(j)  # komplett serialisierbar


def test_stale_json_schema(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Stale-JSON: stale=true + stale_reasons + „beibehalten“-Empfehlungen."""
    rec = recommend("2026-11-01 17:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    j = rec.to_json()
    assert j["stale"] is True and j["stale_reasons"]
    assert all("beibehalten" in e["vorgeschlagen"] for e in j["empfehlungen"])
    assert j["kontext"]["stale"] is True
    json.dumps(j)


def test_excluded_parameters_never_recommended(st_hot: Any, wx_ok: Any, skill_map: Any) -> None:
    """Ölbrenner/Betriebsmodus werden NIE empfohlen (maintainer-Entscheidung;
    v0.5: Minderer-Feinjustierung + O8-Modus SIND Empfehlungen (Advisory /
    cmi-settable-Advisory), Ölbrenner bleibt exkludiert."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st_hot), wx=wx_ok,
                    predictor=FakePredictor("change_kell"), skill_map=skill_map)
    params = {e["parameter"] for e in rec.empfehlungen}
    for excl in ("oel_burner", "Ölbrenner", "Betriebsmodus"):
        assert not any(excl in p for p in params), excl


# --- Determinismus + Rollout-Smoke ---


def test_deterministic_recommendation(st_hot: Any, wx_ok: Any, skill_map: Any) -> None:
    """Zwei Läufe (gleiche Eingaben) → identisches JSON (kein RNG-Drift)."""
    p1 = FakePredictor("change_kell")
    p2 = FakePredictor("change_kell")
    r1 = recommend("2026-11-01 11:00", features_dir=_store_dir(st_hot), wx=wx_ok,
                   predictor=p1, skill_map=skill_map)
    r2 = recommend("2026-11-01 11:00", features_dir=_store_dir(st_hot), wx=wx_ok,
                   predictor=p2, skill_map=skill_map)
    assert r1.to_json() == r2.to_json()


def test_rollout_smoke_fake_predictor(st_hot: Any) -> None:
    """Rollout (FakePredictor): h=0..12 vorhanden, perzipiert = Ist+Offset+Δ."""
    import math

    from analysis.model.l2.recommend import rollout
    from analysis.model.m2b.data import load_store

    p = FakePredictor("change_kell")
    from analysis.model.l2.recommend import OFFSET_STEP_COLS

    tsa, raw, _ = load_store(
        _store_dir(st_hot),
        columns=list(dict.fromkeys(list(p.raw_inputs) + list(OFFSET_STEP_COLS))),
        where="ts = TIMESTAMP '2026-11-01 11:00'",
    )
    raw = {k: v[:1] for k, v in raw.items()}
    tsa = tsa[:1]
    ro = rollout(p, "2026-11-01 11:00", raw, tsa, {"kell": -0.5, "eg": 0.0, "dg": 0.0})
    assert set(ro) == {0, 1, 4, 6, 12}
    # Keller: 25,3 − 0,5 = 24,8 (Offset-Semantik: perzipiert wird mit Δ verschoben).
    assert abs(ro[0]["ras_kell_perc"] - 24.8) < 1e-6
    # EG unverändert: 20,1.
    assert abs(ro[12]["ras_eg_perc"] - 20.1) < 1e-6
    # Alle Targets × Horizonte gefüllt (kein NaN).
    for h in ro:
        for v in ro[h].values():
            assert math.isfinite(v)


def test_save_json_and_report(st: Any, wx_ok: Any, skill_map: Any, tmp_path: Any) -> None:
    """JSON-Datei + REPORT.md: existieren, JSON parsebar, REPORT DE-Struktur."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    out = save_json(rec, rec.ts, runs_dir=tmp_path)
    assert out.exists() and out.name.startswith("recommend_")
    j = json.loads(out.read_text())
    assert j["ts"] == rec.ts
    rep = write_report(rec, out, out_path=tmp_path / "REPORT.md")
    txt = rep.read_text()
    for needle in ("Top-3-Empfehlungen", "Tages-Advisory", "ΔT-Diagnose & Kondition",
                   "Guardrail-Hits", "Executability-Map",
                   "Kosten-Abbildung", "model_version"):
        assert needle in txt, needle
    assert "cmi-brain-v003" in txt


# --- State/Store (Lücke, gap_full) ---


def test_state_missing_row_falls_back(st: Any) -> None:
    """ts-Zeile fehlt (16:00 > letzter Bucket 15:00) → letzter Bucket, Lücke 1 h (kein Stale)."""
    from analysis.model.l2.recommend import load_state

    s2 = load_state("2026-11-01 16:00", features_dir=_store_dir(st))
    assert s2.state_ts_str.startswith("2026-11-01 15:00")
    assert s2.requested_missing is True
    assert s2.metric_gap_h == 1.0


def test_state_gap_full_falls_back(tmp_path: Path) -> None:
    """ts-Zeile mit gap_full (12:00) → vorherige vollständige Stunde (11:00)."""
    import shutil

    import pyarrow as pa
    import pyarrow.parquet as pq

    from analysis.model.l2.recommend import load_state
    from analysis.model.l2.tests.conftest import make_mini_store

    src = make_mini_store(tmp_path / "src_store")
    dest = tmp_path / "gap_store"
    shutil.copytree(src, dest)
    p = dest / "2026" / "11" / "2026-11.parquet"
    table = pq.read_table(p)
    arr = table.column("gap_full").to_pylist()
    arr[1] = 1.0  # 12:00-Zeile lückenbehaftet → Fallback auf 11:00
    table = table.set_column(
        table.schema.get_field_index("gap_full"),
        pa.field("gap_full", pa.float64()),
        pa.array(arr),
    )
    pq.write_table(table, p)
    s = load_state("2026-11-01 12:00", features_dir=dest)
    assert s.state_ts_str.startswith("2026-11-01 11:00")
    assert s.gap_full is False
    assert s.requested_missing is False
    assert s.metric_gap_h == 1.0


def test_state_gap_full_first_row_keeps_gap(tmp_path: Path) -> None:
    """gap_full auf der ÄLTESTEN Zeile (keine vorherige verfügbar) → dieselbe
    Zeile wird genutzt, gap_full-Flag bleibt gesetzt (Stale-Grund)."""
    import shutil

    import pyarrow as pa
    import pyarrow.parquet as pq

    from analysis.model.l2.recommend import load_state
    from analysis.model.l2.tests.conftest import make_mini_store

    src = make_mini_store(tmp_path / "src_store")
    dest = tmp_path / "gap_store"
    shutil.copytree(src, dest)
    p = dest / "2026" / "11" / "2026-11.parquet"
    table = pq.read_table(p)
    arr = table.column("gap_full").to_pylist()
    arr[0] = 1.0
    table = table.set_column(
        table.schema.get_field_index("gap_full"),
        pa.field("gap_full", pa.float64()),
        pa.array(arr),
    )
    pq.write_table(table, p)
    s = load_state("2026-11-01 11:00", features_dir=dest)
    # Älteste Zeile ist selbst lückenbehaftet: kein Fallback, Flag aktiv.
    assert s.state_ts_str.startswith("2026-11-01 11:00")
    assert s.gap_full is True
