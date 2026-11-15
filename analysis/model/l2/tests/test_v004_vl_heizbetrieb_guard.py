"""L2 v0.4 — VL-Soll-Betriebsfenster-Guard (keine Heizkurven-Empfehlung
ohne Heizbetrieb, maintainer-Entscheidung 2026-11-03, s.
docs/l2-v0.4-vl-heizbetrieb-guard.md).

Im Ruhezustand (Öl-Brenner + Heizkreispumpe seit 3 h aus,
``oel_burner_on_sum3h``/``pump_hz_on_sum3h`` < ``VL_HEATING_ACTIVE_EPS``)
hat das VL-Soll **keine Stellkraft** (UVR folgt dem VL-Soll nur, wenn
geheizt wird; Vorlauf kühlt nur passiv ab, Puffer z. B. durch Kamin
gehalten) → keine Absatz-Empfehlung: ``vorgeschlagen`` =
„beibehalten (kein Heizbetrieb)“ + Ruhezustand-Text + Guardrail-Hit.
OR-Semantik: EINER der beiden Signale reicht (Hz-Pumpe an kann ohne
Öl-Brenner vorkommen, z. B. rein Puffer-Umlauf). NULL in einer der
beiden Sum-Spalten → „unbekannt“ → Guard stumm (Fail-Open, exaktes
v0.3-Verhalten). Synthetic-Store + FakePredictor, kein Live-Netz.
"""
from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from analysis.model.l2 import config
from analysis.model.l2.recommend import heating_active, recommend
from analysis.model.l2.tests.conftest import N_ROWS, make_mini_store
from analysis.model.l2.tests.test_v003_ist_kalibrierung import TS, FakePredictorV3


def _set_sum3h(root: Any, oel3: float | None, pump3: float | None) -> Any:
    """Heizbetrieb-Summen (v0.4) je Store-Zeile setzen (Skalar, None = NULL)."""
    p = root / "2026" / "11" / "2026-11.parquet"
    t = pq.read_table(p)
    for name, val in (("oel_burner_on_sum3h", oel3), ("pump_hz_on_sum3h", pump3)):
        vals = [None if val is None else float(val)] * N_ROWS
        t = t.set_column(
            t.schema.get_field_index(name), pa.field(name, pa.float64()),
            pa.array(vals, type=pa.float64()),
        )
    pq.write_table(t, p)
    return root


def _p_eq() -> FakePredictorV3:
    """Gleichgewichts-Predictor (alle Zonen perz 20,0, gain 0) → No-Change."""
    return FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 20.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 0.0},
    )


def _vl_card(rec: Any) -> dict[str, Any]:
    return next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")


def test_heating_active_klassifikation() -> None:
    """Helfer-Unit: OR-Semantik (EPS-Gate) + NULL → None (Fail-Open)."""
    eps = config.VL_HEATING_ACTIVE_EPS
    assert heating_active({"oel_burner_on_sum3h": 0.0, "pump_hz_on_sum3h": 0.0}) is False
    assert heating_active({"oel_burner_on_sum3h": 0.0, "pump_hz_on_sum3h": eps}) is True
    assert heating_active({"oel_burner_on_sum3h": 0.0, "pump_hz_on_sum3h": 1.0}) is True
    assert heating_active({"oel_burner_on_sum3h": 1.0, "pump_hz_on_sum3h": 0.0}) is True
    # NULL/fehlend in EINER der beiden Spalten → unbekannt (NICHT False:
    # ``_h``-Semantik (None → 0,0) würde den Guard fälschlich schalten).
    assert heating_active({"oel_burner_on_sum3h": None, "pump_hz_on_sum3h": 0.0}) is None
    assert heating_active({"pump_hz_on_sum3h": 1.0}) is None
    assert heating_active({}) is None


def test_ruhe_keine_absatz_empfehlung(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-1: Ruhezustand (Brenner + Hz-Pumpe seit 3 h aus, 0,0/0,0) +
    VL unter der Kurve (err < −VL_ERR_HIGH_K) → **keine** „Absatz …
    Raster“-Empfehlung; „beibehalten (kein Heizbetrieb)“; Ruhezustand-Text;
    Guardrail-Hit vorhanden. Default-Store: ext 7,0 → Kurve 47,0,
    hz_rl 36,5 → err −10,5 K (über der Schwelle, ohne Guard → Absatz)."""
    root = _set_sum3h(make_mini_store(tmp_path / "vl_ruhe"), oel3=0.0, pump3=0.0)
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    assert rec.stale is False
    vl = _vl_card(rec)
    assert "Absatz" not in vl["vorgeschlagen"]
    assert vl["vorgeschlagen"] == "beibehalten (kein Heizbetrieb)"
    assert "Kein Heizbetrieb seit 3 h" in vl["begruendung_de"]
    assert "passiv ab" in vl["begruendung_de"]
    assert "Puffer 62 °C" in vl["begruendung_de"]  # buff_top der state-Zeile (ts)
    assert any(
        h.startswith("vl: kein Heizbetrieb") and "0.00/0.00" in h
        for h in rec.guardrail_hits
    )
    # Tier-B-Advisory bleibt vorhanden (nur Empfehlung unterdrückt,
    # keine Card entfernt).
    assert vl["tier"] == "B"


def test_heizbetrieb_absatz_wie_v003(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-2 (Regressionsschutz): Brenner-3h = 1,0 (aktiv) + VL deutlich
    unter der Kurve → Absatz-Empfehlung (−1 Raster), KEIN Guardrail-Hit.

    v0.5 Ticket C: die Raster-Richtung ist unverändert (−1 Raster), die
    Begründung kommt jetzt aus der **VL-Soll-Kalibrierung** (Cache fehlt im
    Test → v0.4-Heuristik-Fallback + „Kalibrierung nicht verfügbar“-Hinweis;
    DoD-C-Regression: nur der Ruhe-Zustand-Text muss Wort-für-Wort
    unverändert bleiben). ext 9,0 °C → Kurve 45,7 °C; Ist-VL 32,0 °C →
    dev −13,7 K → −1 Raster, Ziel-VL-Soll = 44,7 °C."""
    root = _set_sum3h(make_mini_store(tmp_path / "vl_haz"), oel3=1.0, pump3=0.0)
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    assert rec.stale is False
    vl = _vl_card(rec)
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    assert "Kalibrierung nicht verfügbar" in vl["begruendung_de"]
    assert "Ziel-VL-Soll bei Außen 9.0 °C = 44.7 °C" in vl["begruendung_de"]
    assert not any(h.startswith("vl: kein Heizbetrieb") for h in rec.guardrail_hits)


def test_pumpe_only_aktiv(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """OR-Semantik: Hz-Pumpe an (3-h-Summe 1,0), Öl-Brenner aus (0,0) →
    aktiv → v0.3-Verhalten (Absatz-Empfehlung, kein Guard)."""
    root = _set_sum3h(make_mini_store(tmp_path / "vl_pump"), oel3=0.0, pump3=1.0)
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    assert _vl_card(rec)["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    assert not any(h.startswith("vl: kein Heizbetrieb") for h in rec.guardrail_hits)


def test_null_fail_open(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """DoD-3: beide Sum-Spalten NULL (Default-Store) → Guard stumm →
    exaktes v0.3-Verhalten (Absatz-Empfehlung, KEIN Guardrail-Hit,
    kein Ruhezustand-Text)."""
    root = make_mini_store(tmp_path / "vl_null")
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    vl = _vl_card(rec)
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    assert "Kein Heizbetrieb seit 3 h" not in vl["begruendung_de"]
    assert not any(h.startswith("vl: kein Heizbetrieb") for h in rec.guardrail_hits)


def test_model_version_l2_v004_und_determinismus(
    tmp_path: Any, wx_ok: Any, skill_map: Any
) -> None:
    """DoD-4: model_version = cmi-brain-v003+l2-v005 (v0.5-Identität, Ticket D;
    v0.4-Guard-Baseline) + Determinismus (zwei Läufe → identisches JSON,
    inkl. Guard-Texte)."""
    root = _set_sum3h(make_mini_store(tmp_path / "vl_v4"), oel3=0.0, pump3=0.0)
    rec1 = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    rec2 = recommend(TS, features_dir=root, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map)
    assert config.L2_VERSION == "l2-v005"
    assert rec1.model_version == "cmi-brain-v003+l2-v005"
    assert rec1.to_json()["model_version"] == "cmi-brain-v003+l2-v005"
    assert rec1.to_json() == rec2.to_json()
