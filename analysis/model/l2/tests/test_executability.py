"""L2 v0.2 — dynamische Executability-Markierung (discovery-basiert).

Tests für ``l2.executability`` + die Resolver-Integration in
``recommend()``:

- je-Typ-Wert + ``cmi_hint`` (kuratiertes Mapping, realer INVENTAR.json);
- Fail-Closed bei fehlendem/ungültigem INVENTAR (advisory, **nie**
  ``cmi-settable``);
- RISK-blocked-Pfad (aus Discovery-``risk``-Flag, nie Adresse im Ergebnis);
- Determinismus, ``model_version=l2-v002``;
- L2 emittiert KEIN ``adresse``/``to_value`` (UI-„Anwenden“ bleibt inaktiv).

0 Live-Netz: Discovery = Datei-Lesung mit tmp-Fixtur, Open-Meteo gemockt
(``WxLive``-Fixtur, wie v0.1).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from analysis.model.l2 import config
from analysis.model.l2.executability import (
    Discovery,
    DiscoveryEntry,
    executability_for,
    load_discovery,
)
from analysis.model.l2.recommend import recommend
from analysis.model.l2.tests.conftest import FakePredictor

# --- Discovery-Fixtur (aus dem realen Repo-INVENTAR.json, read-only) ---


@pytest.fixture(scope="module")
def real_inventar_path() -> Any:
    return config.INVENTAR_DEFAULT_PATH


@pytest.fixture()
def real_discovery(real_inventar_path: Any) -> Any:
    d = load_discovery(real_inventar_path)
    assert d is not None, "Repo-INVENTAR.json muss lesbar sein"
    return d


def _store_dir(st: Any) -> Any:
    return st.store_dir


# --- 1) je-Typ-Wert + cmi_hint (kuratiertes Mapping, reales Discovery) ---


@pytest.mark.parametrize(
    ("parameter", "expected", "q_needle"),
    [
        ("offset_kell", config.EXECUTABILITY_ADVISORY_UVR, "Q-107"),
        ("offset_eg", config.EXECUTABILITY_ADVISORY_UVR, "Q-107"),
        ("offset_dg", config.EXECUTABILITY_ADVISORY_UVR, "Q-107"),
        ("vl_curve", config.EXECUTABILITY_ADVISORY_TA_DESIGNER, "Q-108"),
        ("ww_window", config.EXECUTABILITY_ADVISORY_ZEITPROGRAMM, "Zeitprogramm"),
        ("circulation", config.EXECUTABILITY_ADVISORY_UVR, "UVR"),
    ],
)
def test_executability_per_type_and_hint(
    parameter: str, expected: str, q_needle: str, real_discovery: Any
) -> None:
    ex, hint = executability_for(parameter, None, real_discovery)
    assert ex == expected
    assert q_needle in hint
    assert ex != config.EXECUTABILITY_CMI_SETTABLE
    # WW-Ladefenster ↔ CMI-Schalter „WW vom Oel“ (writeable=true) →
    # Discovery-Bezug im Hint (schreibbar, aber L2 empfiehlt bewusst nicht).
    if parameter == "ww_window":
        assert "WW vom Oel" in hint
        assert "cmi-setzbar" in hint


def test_offset_not_in_schematic_write_path(real_discovery: Any) -> None:
    """RAS-Offset: NICHT unter den 9 Schematic-Adressen (Q-107) → Advisory
    ohne CMI-Schalter-Bezug (kein `cmi-setzbar` im Hint)."""
    ex, hint = executability_for("offset_kell", "kell", real_discovery)
    assert ex == config.EXECUTABILITY_ADVISORY_UVR
    assert "cmi-setzbar" not in hint
    assert "Q-107" in hint


# --- 2) Fail-Closed (fehlend/ungültig → advisory, nie cmi-settable) ---


def test_load_discovery_missing_returns_none(tmp_path: Any) -> None:
    assert load_discovery(tmp_path / "does-not-exist.json") is None


def test_load_discovery_invalid_json_returns_none(tmp_path: Any) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_discovery(p) is None


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"entries": []}, "leere entries"),
        ({"no_entries": 1}, "entries fehlt"),
        (["nur", "liste"], "Root keine dict"),
        ({"entries": [{}]}, "Eintrag ohne address"),
        ({"entries": [1, 2]}, "Eintrag kein dict"),
        ({"entries": [{"address": "1", "risk": "true", "writeable": True}]},
         "risk nicht boolean"),
        ({"entries": [{"address": "1", "risk": True, "writeable": True,
                       "option_values": "select"}]}, "option_values ungültig"),
    ],
)
def test_load_discovery_malformed_returns_none(tmp_path: Any, payload: Any, why: str) -> None:
    p = tmp_path / "malformed.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert load_discovery(p) is None, why


def test_fail_closed_missing_inventar_all_advisory(
    st: Any, wx_ok: Any, skill_map: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    """INVENTAR fehlt (Env auf tmp-Pfad) → alle advisory, nie pending/cmi-settable.

    v0.5-rework (2026-11-12): O8-Block ist eine Statuszeile
    (``advisory-manuell``, kein CMI-Write-Pfad) → ohne Discovery bleibt
    ``advisory-manuell`` (fail-closed, nie cmi-settable, nie pending)."""
    monkeypatch.setenv("CMI_WRITE_INVENTAR", str(tmp_path / "missing.json"))
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    for e in rec.empfehlungen:
        assert e["executability"] != config.EXECUTABILITY_PENDING, e["parameter"]
        assert e["executability"] != config.EXECUTABILITY_CMI_SETTABLE, e["parameter"]
        if e["parameter"] == "Pumpe Heizkreis O8 (Modus)":
            assert e["executability"] == config.EXECUTABILITY_ADVISORY_MANUELL, e["parameter"]
        assert e.get("cmi_hint")


def test_executability_for_discovery_none_fallback() -> None:
    """``discovery=None`` (Fail-Closed): advisory-Label + Q-Bezug (kein
    Discovery-Dateibezug, „fehlend“), nie cmi-settable; unbekannt → PENDING."""
    ex, hint = executability_for("offset_eg", "eg", None)
    assert ex == config.EXECUTABILITY_ADVISORY_UVR
    assert "Q-107" in hint and "fehlend" in hint
    ex2, hint2 = executability_for("nicht_im_mapping", None, None)
    assert ex2 == config.EXECUTABILITY_PENDING  # unbekannt → Fail-Closed-Fallback
    assert hint2  # kuratierter Offen-Hint


def test_executability_for_unknown_parameter_fallback() -> None:
    ex, _hint = executability_for("nicht_im_mapping", None, None)
    assert ex == config.EXECUTABILITY_PENDING
    # Nicht-Discovery-Eingabe (z. B. versehentlicher String) → Fail-Closed,
    # kein Crash (isinstance-Check).
    ex2, _h2 = executability_for("offset_dg", "dg", "kein-discovery-objekt")
    assert ex2 == config.EXECUTABILITY_ADVISORY_UVR


# --- 3) RISK-blocked (aus Discovery risk-Flag; keine Adresse im Ergebnis) ---


def test_risk_blocked_path() -> None:
    """Parameter einem RISK-Schalter (Ölbrenner) zugeordnet → risk-blocked
    (niemals setzbar), abgeleitet aus dem Discovery-``risk``-Flag."""
    disc = Discovery.parse(
        {
            "entries": [
                {
                    "address": "10520A10180",
                    "label": "Oelbrenner (Anfoerdung)",
                    "risk": True,
                    "writeable": False,
                    "option_values": ["1", "2", "3"],
                    "current_value": "3",
                },
                {
                    "address": "10720A10180",
                    "label": "Zirkulation",
                    "risk": False,
                    "writeable": True,
                    "option_values": ["1", "2", "3"],
                    "current_value": "3",
                },
            ]
        },
        Path("fixture:inventar-36.json"),
    )
    assert [e.address for e in disc.risk_entries] == ["10520A10180"]
    # Ölbrenner (kuratiert als exkludierter Parameter mit Discovery-Zuordnung)
    # ↔ RISK-Schalter → risk-blocked, nie cmi-settable/advisory.
    ex, hint = executability_for("oel_burner_on", None, disc)
    assert ex == config.EXECUTABILITY_RISK_BLOCKED
    assert "RISK" in hint and "niemals setzbar" in hint
    # Nicht-RISK-Zuordnung bleibt advisory (writeable-Schalter, nicht empfohlen).
    ex2, hint2 = executability_for("circulation", None, disc)
    assert ex2 == config.EXECUTABILITY_ADVISORY_UVR
    assert "cmi-setzbar" in hint2


def test_risk_entry_not_writeable_but_never_emitted() -> None:
    """RISK-Adressen stehen im Discovery, werden aber von L2 nie als
    Schalter-Empfehlung emittiert (maintainer-Option 2) — nur der Schutz wirkt."""
    assert DiscoveryEntry.from_dict(
        {"address": "1", "risk": True, "writeable": False}
    ).writeable is False


# --- 4) Determinismus + model_version + Fail-Closed-Ende-zu-Ende ---


def test_deterministic_executability(st_full: Any, wx_ok: Any, skill_map: Any) -> None:
    """Zwei Läufe (gleiche Eingaben, realer INVENTAR) → identische
    Executability-Markierung (kein RNG/Zeit-Drift)."""
    p1, p2 = FakePredictor(), FakePredictor()
    r1 = recommend("2026-11-01 11:00", features_dir=_store_dir(st_full), wx=wx_ok,
                   predictor=p1, skill_map=skill_map)
    r2 = recommend("2026-11-01 11:00", features_dir=_store_dir(st_full), wx=wx_ok,
                   predictor=p2, skill_map=skill_map)
    mark1 = [(e["parameter"], e["executability"], e["cmi_hint"]) for e in r1.empfehlungen]
    mark2 = [(e["parameter"], e["executability"], e["cmi_hint"]) for e in r2.empfehlungen]
    assert mark1 == mark2
    assert r1.to_json() == r2.to_json()


def test_model_version_l2_v004(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """v0.5: versionierte Identität (l2-v004 → l2-v005, Ticket D)."""
    assert config.L2_VERSION == "l2-v005"
    assert config.model_version() == "cmi-brain-v003+l2-v005"
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    assert rec.model_version == "cmi-brain-v003+l2-v005"
    assert rec.to_json()["model_version"] == "cmi-brain-v003+l2-v005"


def test_fail_closed_end_to_end_stale(
    st: Any, wx_old: Any, skill_map: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    """Stale + fehlendes INVENTAR: auch der Stale-Pfad markiert advisory
    (nie pending, nie cmi-settable)."""
    monkeypatch.setenv("CMI_WRITE_INVENTAR", str(tmp_path / "missing.json"))
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_old,
                    predictor=FakePredictor(), skill_map=skill_map)
    assert rec.stale is True
    for e in rec.empfehlungen:
        assert e["executability"] != config.EXECUTABILITY_PENDING
        assert e["executability"] != config.EXECUTABILITY_CMI_SETTABLE


# --- 5) Keine Adresse: L2 emittiert kein `adresse`/`to_value` ---


def test_recommendation_json_has_no_adresse_or_to_value(st: Any, wx_ok: Any, skill_map: Any) -> None:
    """Keine Empfehlung trägt ``adresse``/``to_value`` (v0.5-rework
    2026-11-12: O8-Block ist eine **Statuszeile** — ``advisory-manuell``,
    KEIN ``to_value``/CMI-Write-Pfad; ``o8`` = {adresse, optionen,
    aktueller_modus} dient nur der Anzeige). Alle Empfehlungen bleiben
    advisory ohne Write-Pfad (UI-„Anwenden“ inaktiv)."""
    rec = recommend("2026-11-01 11:00", features_dir=_store_dir(st), wx=wx_ok,
                    predictor=FakePredictor(), skill_map=skill_map)
    j = rec.to_json()
    for e in j["empfehlungen"]:
        assert "adresse" not in e
        assert "to_value" not in e
        assert e["executability"] != "cmi-settable"
    o8s = [e for e in j["empfehlungen"] if e["parameter"] == "Pumpe Heizkreis O8 (Modus)"]
    if o8s:
        o8 = o8s[0]
        assert o8["executability"] == "advisory-manuell"
        assert o8["o8"]["adresse"] == config.O8_ADDRESS
        assert o8["o8"]["aktueller_modus"] == "AUTO"
        # Statuszeile: kein to_value (kein Wechsel empfohlen), Optionen nur
        # für die Anzeige (Auswahl 1/2/3 = AUS/EIN/AUTO).
        assert "to_value" not in o8["o8"]
        assert set(o8["o8"]["optionen"]) == {"1", "2", "3"}


def test_ui_executable_gate_false_for_all_advisory_labels() -> None:
    """Kein v0.2-Label gehört zur UI-EXECUTABLE-Menge (ki.js) → Badge
    „Advisory (TA-Designer/v2)“, kein Anwenden-Button."""
    ui_executable = {"setzbar-per-cmi", "cmi-settable", "whitelist"}
    for label in (
        config.EXECUTABILITY_ADVISORY_UVR,
        config.EXECUTABILITY_ADVISORY_TA_DESIGNER,
        config.EXECUTABILITY_ADVISORY_ZEITPROGRAMM,
        config.EXECUTABILITY_RISK_BLOCKED,
        config.EXECUTABILITY_PENDING,
    ):
        assert label not in ui_executable, label
        assert re.fullmatch(r"[a-z0-9-]+", label) is not None


# --- Discovery-Loader (Pfad-Auflösung: Env → Default; Eintrags-Validierung) ---


def test_inventar_path_env_and_default(monkeypatch: Any) -> None:
    from analysis.model.l2.executability import inventar_path

    monkeypatch.delenv("CMI_WRITE_INVENTAR", raising=False)
    assert inventar_path() == config.INVENTAR_DEFAULT_PATH
    monkeypatch.setenv("CMI_WRITE_INVENTAR", "/srv/cmi/scripts/cmi_write_discovery/INVENTAR.json")
    assert str(inventar_path()).endswith("INVENTAR.json")


def test_real_inventar_has_expected_entries(real_inventar_path: Any) -> None:
    """Real-Abgleich (Stand 2026-11-07, Seite-6-Erweiterung): 36 Adressen,
    O8 „Pumpe Heizkreis“ writeable + non-RISK (v0.5 Ticket B cmi-settable-
    Basis), Ölbrenner RISK (alt). RISK-Schalter sind nie writeable."""
    raw = json.loads(real_inventar_path.read_text(encoding="utf-8"))
    entries = raw["entries"]
    assert len(entries) == 36
    o8 = next(e for e in entries if e["address"] == "10820A10180")
    assert o8["risk"] is False and o8["writeable"] is True
    assert o8["option_values"] == ["1", "2", "3"]
    assert o8["current_value"] in ("1", "2", "3")
    risk = [e for e in entries if e.get("risk")]
    assert any(e["address"] == "10520A10180" for e in risk)  # Ölbrenner (alt)
    # RISK-Schalter sind nie writeable (S3-1-Schutz):
    for e in entries:
        if e.get("risk"):
            assert e.get("writeable") is False
