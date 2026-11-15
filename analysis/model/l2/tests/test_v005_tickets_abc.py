"""L2 v0.5 Tickets A/B/C — Minderer / Pumpe O8 / Heizkurven-Kalibrierung.

Tests für die neuen Empfehlungs-Blocks (``analysis.model.l2.recommend`` +
``vl_calibration``):

- **Ticket A (Minderer, v0.5-engine-rework 2026-11-12):** „Ist-Vorgabe“ =
  Sollspannung a17/18/19 (statische Vorgabe, ``per_zone.minderer_soll_v``,
  Default 10,0 V) statt off2×10 V; Gate „≥ 2 aktive Kreise“ ENTFERNT (O8
  regelt den Durchfluss); V-Ziel linear (mehr ΔT-Fehlmenge → mehr drosseln,
  Klemme 0 V), ΔT-OK → „beibehalten“, ΔT > Ziel → „öffnen“, ΔT-None-Zweig
  („ΔT unbekannt“), Kondition inaktiv → stumm.
- **Ticket B (Pumpe O8, v0.5-engine-rework):** Statuszeile statt Empfehlung
  („Modus AUTO (an/aus nach Anforderung)“, „AUTO beibehalten — Stufe/
  Druck-Soll manuell am Aggregat“), ``executability = advisory-manuell``,
  KEIN ``to_value``/``executability_mix``, ``o8`` = {adresse, optionen,
  aktueller_modus}; Gating (Konditions-Flag) + Fail-Closed ohne Discovery.
- **Ticket C (Heizkurve, v0.5-engine-rework):** Klartext in
  ``aktuell``/``vorgeschlagen`` („Ist-VL … → VL ~x K zu hoch“, „1 Raster
  absenken → ~0,5–1 K weniger VL-Soll“), Jargon (Raster/Kalibrierung/n) im
  ``detail``-Feld (= ``begruendung_de``); VL-Kalibrierung (Heizstunde-Maske +
  Außen-T-Bänder + min_n-Usability), Cache-Fallback („Kalibrierung nicht
  verfügbar“ + v0.4-Heuristik), Raster-Logik + Ziel-VL-Soll-Text;
  v0.4-Ruhezustand bleibt unverändert (getrennt in ``test_v004_*``).
- **RAS-Offset (v0.5-engine-rework):** De-Emphasis — ``raum_text``-Feld
  prominent, Handlung-Einzeiler „Raum-Soll bei Bedarf direkt im
  UVR/TA-Designer setzen (Offset intern)“, state-Präfix + Guard-Texte
  bleiben.

Synthetische Stores/Artefakte, kein Live-Netz, kein CMI-Zugriff,
deterministisch (kein RNG).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.l2 import config
from analysis.model.l2 import vl_calibration as vl_cal
from analysis.model.l2.executability import load_discovery
from analysis.model.l2.recommend import (
    State,
    minderer_v_target_v,
    recommend,
    tier_b_advisories,
)
from analysis.model.l2.tests.conftest import N_ROWS, make_mini_store
from analysis.model.l2.tests.test_v003_ist_kalibrierung import TS, FakePredictorV3

# --- Hilfskonstrukte --------------------------------------------------------


def _state(**over: Any) -> State:
    """Minimal-State (wie ``test_cli._canned_state``) + v0.4-3-h-Summen."""
    rows = {
        "oel_burner_on_sum3h": 1.0,
        "pump_hz_on_sum3h": 0.0,
        "ext": 13.0,
        "hz_vl": 40.0,
        "hz_rl": 42.0,
        "buff_top": 55.0,
        "solar_coll": 28.0,
        "pump_circ_on_sum24h": 0.0,
    }
    rows.update(over)
    return State(
        ts_str="2026-11-01 11:00",
        ts=datetime(2026, 11, 1, 11, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        rows=rows,
        state_ts_str="2026-11-01 11:00:00",
        data_version="m1.3",
        store_last_ts="2026-11-01 15:00:00",
        metric_gap_h=0.0,
        requested_missing=False,
        gap_full=False,
    )


def _diag(rm_open: dict[str, float], per_zone: dict[str, float],
          active: bool | None = None,
          soll_v: float | None = config.RM_MINDERER_SOLL_DEFAULT_V) -> dict[str, Any]:
    """Ticket-D-Diagnose-Dict (``RmDiagnostics.to_dict()``-Form) synthetisch.

    ``rm_open``: Minderer-offen-2-h-Ø je Zone (Aktiv-Semantik ≥ 0,5).
    ``per_zone``: ΔT (lag-korrigiert) je Zone (None = nicht ermittelt).
    ``active``: Konditions-Flag (Default: aktiv, wenn ≥ 1 Zone offen ≥ 0,5).
    ``soll_v``: Minderer-Sollspannung je Zone (a17–19, statische Vorgabe,
    30-min-Median); Default 10,0 V. ``None`` = Feld fehlt im Payload →
    Engine-Default (Legacy-Simulation).
    """
    if active is None:
        active = any(v >= config.RM_ACTIVE_QUOTA for v in rm_open.values())
    active_zones = [z for z in config.ZONES if rm_open.get(z, 0.0) >= config.RM_ACTIVE_QUOTA]
    per_zone_full: dict[str, dict[str, Any]] = {}
    for z in config.ZONES:
        entry: dict[str, Any] = {
            "lag_s": 30,
            "lag_confidence": 0.5,
            "lag_source": "estimate",
            "delta_t_c": per_zone.get(z),
            "delta_t_target_c": config.RM_DELTA_T_TARGET_C,
            "delta_t_gap_c": (
                None if per_zone.get(z) is None
                else config.RM_DELTA_T_TARGET_C - per_zone[z]
            ),
            "flow_class": "offen" if rm_open.get(z, 0.0) >= 0.5 else "zu",
            "rm_open_2h": rm_open.get(z, 0.0),
            "n_raw_samples": 7200,
        }
        if soll_v is not None:
            entry["minderer_soll_v"] = soll_v
        per_zone_full[z] = entry
    return {
        "active": active,
        "condition": {
            "active": active,
            "pump_3h_avg": 0.0,
            "rm_open_2h": rm_open,
            "ras_gap": {z: 0.0 for z in config.ZONES},
            "active_zones": active_zones,
            "reasons": [],
        },
        "delta_t_target_c": config.RM_DELTA_T_TARGET_C,
        "raw_window_s": 7200,
        "lag_source": {z: "estimate" for z in config.ZONES},
        "per_zone": {} if not active else per_zone_full,
    }


def _p_eq() -> FakePredictorV3:
    return FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 20.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 0.0},
    )


def _real_discovery() -> Any:
    d = load_discovery(config.INVENTAR_DEFAULT_PATH)
    assert d is not None, "Repo-INVENTAR.json muss lesbar sein"
    return d


def _o8_block(adv: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in adv if e["parameter"] == "Pumpe Heizkreis O8 (Modus)"]


def _minderer_block(adv: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in adv if e["parameter"].startswith("Minderer-Feinjustierung")]


# --- Ticket A: Minderer-Empfehlung -----------------------------------------


def test_minderer_v_target_monotonic_and_capped() -> None:
    """DoD-A: V-Ziel-Monotonie — größere ΔT-Fehlmenge (kleinerer ΔT) →
    kleineres V-Ziel (mehr schließen), Klemme 0 V (max. zu); ΔT ≥ Ziel →
    kein Schließen (10 V)."""
    tgt = config.RM_DELTA_T_TARGET_C  # 10,0
    v0 = minderer_v_target_v(0.0, 10.0, tgt)
    v4 = minderer_v_target_v(4.0, 10.0, tgt)
    v6 = minderer_v_target_v(6.0, 10.0, tgt)
    v8 = minderer_v_target_v(8.0, 10.0, tgt)
    # ΔT-Fehlmenge 10 → 6 → 4 → 2 K: V-Ziel 0 → 4 → 6 → 8 V (monoton steigend
    # in ΔT = monoton schließend in der Fehlmenge).
    assert v0 == pytest.approx(0.0, abs=1e-9)      # ΔT 0 → max. zu (Klemme)
    assert v4 == pytest.approx(4.0, abs=1e-9)      # linear: 10·(1 − 6/10)
    assert v6 == pytest.approx(6.0, abs=1e-9)
    assert v8 == pytest.approx(8.0, abs=1e-9)
    assert v0 <= v4 <= v6 <= v8 <= config.RM_V_MAX
    # ΔT ≥ Ziel → kein Schließen (V-Ziel = max. 10 V):
    assert minderer_v_target_v(10.0, 10.0, tgt) == config.RM_V_MAX
    assert minderer_v_target_v(15.0, 10.0, tgt) == config.RM_V_MAX
    # Klemme unter 0 (ΔT < 0, physikalisch absurd) → 0 V:
    assert minderer_v_target_v(-5.0, 10.0, tgt) == 0.0
    # Aktuelle V über 10 V (Skalierungs-Rand) → geklemmt:
    assert minderer_v_target_v(4.0, 10.5, tgt) == pytest.approx(4.0, abs=1e-9)
    # Proportional zur aktuellen Stellung (bei gleicher Fehlmenge):
    assert minderer_v_target_v(4.0, 5.0, tgt) == pytest.approx(2.0, abs=1e-9)


def test_minderer_zwei_kreise_v_ziele() -> None:
    """v0.5-rework (engine 2026-11-12): ΔT < Ziel → „Sollspannung runter
    (drosseln)“, ΔT > Ziel + Toleranz → „in Richtung 10 V öffnen“; Gate
    „≥ 2 aktive Kreise“ ist ENTFERNT (O8 regelt den Durchfluss, auch
    1 Minderer kann regeln). V-Ziel linear aus der ΔT-Fehlmenge (Sollspannung
    als Ist-Vorgabe statt off2×10 V)."""
    diag = _diag(
        {"kell": 1.0, "eg": 1.0, "dg": 0.0},
        {"kell": 5.0, "eg": 12.0, "dg": 10.0},
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    blocks = {e["parameter"]: e for e in _minderer_block(adv)}
    assert set(blocks) == {"Minderer-Feinjustierung kell", "Minderer-Feinjustierung eg"}
    # kell: ΔT 5 K < Ziel 10 K → drosseln: V-Ziel = 10,0 V · (1 − 5/10) = ~5,0 V.
    kell = blocks["Minderer-Feinjustierung kell"]
    assert "Sollspannung runter (drosseln)" in kell["vorgeschlagen"]
    assert "~5.0 V" in kell["vorgeschlagen"]
    assert "linear geschätzt" in kell["vorgeschlagen"]
    assert kell["executability"] == config.EXECUTABILITY_ADVISORY_UVR
    assert "Q-108-Analog" in kell["cmi_hint"]
    assert kell["tier"] == "B" and "vl_gate" not in kell
    assert kell["zone"] == "kell"
    assert kell["konfidenz"] == 0.3
    # Ist-Vorgabe = Sollspannung (statische Vorgabe) statt off2×10 V:
    assert kell["aktuell"].startswith("Sollspannung 10.0 V")
    assert "statische Vorgabe" in kell["aktuell"]
    assert "Minderer aktuell auf" in kell["aktuell"]
    assert "offen-2-h-Ø 1.00" in kell["aktuell"]
    assert "linear" in kell["begruendung_de"] and "v0.6" in kell["begruendung_de"]
    # eg: ΔT 12 K > Ziel + 0,5 K → öffnen (in Richtung 10 V).
    eg = blocks["Minderer-Feinjustierung eg"]
    assert "Sollspannung in Richtung 10 V öffnen" in eg["vorgeschlagen"]
    assert "10 V" in eg["wirkung_prognose"] and "voll offen" in eg["wirkung_prognose"]


def test_minderer_dt_im_ziel_beibehalten() -> None:
    """DoD-A: ΔT im Ziel (± ``RM_DT_OK_TOL_K`` = 0,5 K) → „beibehalten“."""
    diag = _diag(
        {"kell": 1.0, "eg": 0.5, "dg": 0.0},
        {"kell": 9.8, "eg": 10.2, "dg": 9.0},
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    blocks = {e["parameter"]: e for e in _minderer_block(adv)}
    assert "beibehalten" in blocks["Minderer-Feinjustierung kell"]["vorgeschlagen"]
    assert "beibehalten" in blocks["Minderer-Feinjustierung eg"]["vorgeschlagen"]


def test_minderer_ein_kreis_auch_v_ziel() -> None:
    """v0.5-rework: Gate „≥ 2 aktive Kreise“ ENTFERNT — auch NUR 1 aktiver
    Kreis bekommt ein V-Ziel (die Druckregelpumpe O8 regelt den Durchfluss;
    auch 1 Minderer kann ihn regeln). Aktiv-Kriterium = rm_open_2h ≥
    RM_ACTIVE_QUOTA je Kreis (Konditions-Flag unangetastet)."""
    diag = _diag(
        {"kell": 1.0, "eg": 0.0, "dg": 0.0},
        {"kell": 5.0, "eg": 40.0, "dg": 40.0},
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    blocks = _minderer_block(adv)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["parameter"] == "Minderer-Feinjustierung kell"
    # V-Ziel auch bei nur 1 aktivem Kreis (kein Gate-Hinweis mehr):
    assert "Sollspannung runter (drosseln)" in b["vorgeschlagen"]
    assert "~5.0 V" in b["vorgeschlagen"]
    assert "1 aktiver Kreis" not in b["vorgeschlagen"]
    assert "wirkungslos" not in b["begruendung_de"]
    assert "vl_gate" not in b


def test_minderer_soll_v_6v_und_fallback() -> None:
    """v0.5-rework: V-Ziel skaliert mit der Ist-Vorgabe = Sollspannung
    (per_zone.minderer_soll_v); bei fehlendem Feld (Legacy-Payload) →
    Default 10,0 V (kein Crash, deterministischer Fallback)."""
    # Sollspannung 6,0 V → V-Ziel = 6,0 · (1 − 5/10) = ~3,0 V:
    diag6 = _diag(
        {"kell": 1.0, "eg": 0.0, "dg": 0.0},
        {"kell": 5.0, "eg": 40.0, "dg": 40.0},
        soll_v=6.0,
    )
    adv6 = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag6)
    b6 = _minderer_block(adv6)[0]
    assert b6["aktuell"].startswith("Sollspannung 6.0 V")
    assert "Sollspannung runter (drosseln): 6.0 V → ~3.0 V" in b6["vorgeschlagen"]
    # Feld fehlt im Payload (Legacy) → Default 10,0 V:
    diag_legacy = _diag(
        {"kell": 1.0, "eg": 0.0, "dg": 0.0},
        {"kell": 5.0, "eg": 40.0, "dg": 40.0},
        soll_v=None,
    )
    adv_lg = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag_legacy)
    b_lg = _minderer_block(adv_lg)[0]
    assert b_lg["aktuell"].startswith(
        f"Sollspannung {config.RM_MINDERER_SOLL_DEFAULT_V:.1f} V"
    )
    assert "10.0 V → ~5.0 V" in b_lg["vorgeschlagen"]


def test_minderer_dt_none_zweig() -> None:
    """v0.5-rework: ΔT nicht ermittelt (None, z. B. kein Roh-Fenster +
    Store-NULL) → „Sollspannung beibehalten (ΔT unbekannt, …)“ (kein V-Ziel,
    kein Crash); aktiv bleibt (offen-Ø ≥ Quorum)."""
    diag = _diag(
        {"kell": 1.0, "eg": 0.0, "dg": 0.0},
        {"kell": None, "eg": 40.0, "dg": 40.0},
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    blocks = _minderer_block(adv)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["parameter"] == "Minderer-Feinjustierung kell"
    assert "Sollspannung beibehalten (ΔT unbekannt, ~10.0 V)" in b["vorgeschlagen"]
    assert "keine Vorgabe-Änderung (ΔT nicht ermittelt)" in b["wirkung_prognose"]
    assert "ΔT nicht ermittelt" in b["begruendung_de"]
    assert "v0.6" in b["begruendung_de"]
    assert "10.0 V" in b["aktuell"]


def test_minderer_kondition_inaktiv_stumm() -> None:
    """DoD-A/B-C-Gating: Konditions-Flag inaktiv → Minderer-Block stumm
    (keine Einträge), auch wenn einzelne Zonen ΔT-Daten hätten."""
    diag = _diag(
        {"kell": 0.0, "eg": 0.0, "dg": 0.0},
        {"kell": 5.0, "eg": 5.0, "dg": 5.0},
        active=False,
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    assert _minderer_block(adv) == []
    assert _o8_block(adv) == []  # B-Block ebenfalls stumm (Gating)
    # und bei fehlender Diagnose (None) → stumm:
    assert _minderer_block(tier_b_advisories(_state(), None, [], _real_discovery())) == []


def test_minderer_pumpe_aktiv_alle_kreise_zu() -> None:
    """Kondition aktiv NUR über Pumpen-Signal (alle Minderer zu) → keine
    aktiven Kreise → Minderer-Block leer (kein Kreis „braucht Wärme“ im
    Minderer-Sinn), O8-Block läuft (Gating = Konditions-Flag)."""
    diag = _diag(
        {"kell": 0.0, "eg": 0.0, "dg": 0.0},
        {"kell": 5.0, "eg": 5.0, "dg": 5.0},
        active=True,
    )
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    assert _minderer_block(adv) == []
    assert len(_o8_block(adv)) == 1


# --- Ticket B: Pumpen-Empfehlung O8 -----------------------------------------


def test_o8_gating_kein_heizbedarf_kein_block() -> None:
    """DoD-B: Gating (kein Heizbedarf → kein Block)."""
    diag_in = _diag({"kell": 0.0, "eg": 0.0, "dg": 0.0},
                    {"kell": 5.0, "eg": 5.0, "dg": 5.0}, active=False)
    assert _o8_block(tier_b_advisories(_state(), None, [], _real_discovery(),
                                       rm_diag=diag_in)) == []
    assert _o8_block(tier_b_advisories(_state(), None, [], _real_discovery(),
                                       rm_diag=None)) == []


def test_o8_statuszeile_advisory_manuell() -> None:
    """v0.5-rework (engine 2026-11-12): O8-Block ist eine **Statuszeile**
    statt Empfehlung (Pumpe schaltet an/aus nach Anforderung; Stufung/
    Druck-Soll manuell am Aggregat) → ``executability = advisory-manuell``,
    **kein** ``to_value``/``executability_mix``; ``o8`` = {adresse,
    optionen, aktueller_modus} (nur Anzeige)."""
    diag = _diag({"kell": 1.0, "eg": 0.0, "dg": 0.0},
                 {"kell": 5.0, "eg": 40.0, "dg": 40.0})
    adv = tier_b_advisories(_state(), None, [], _real_discovery(), rm_diag=diag)
    blocks = _o8_block(adv)
    assert len(blocks) == 1
    o8 = blocks[0]
    # Status statt Empfehlung:
    assert o8["aktuell"].startswith("Modus AUTO")
    assert "an/aus nach Anforderung" in o8["aktuell"]
    assert o8["vorgeschlagen"].startswith("AUTO beibehalten")
    assert "Stufe/Druck-Soll manuell am Aggregat" in o8["vorgeschlagen"]
    # Executability: advisory-manuell (kein cmi-settable, kein Write-Pfad).
    assert o8["executability"] == config.EXECUTABILITY_ADVISORY_MANUELL
    assert "to_value" not in o8
    assert "executability_mix" not in o8
    # o8-Objekt: nur Anzeige (Adresse/Optionen/aktueller Modus aus Discovery).
    assert o8["o8"]["aktueller_modus"] == "AUTO"
    assert o8["o8"]["adresse"] == config.O8_ADDRESS
    assert o8["o8"]["optionen"] == {"1": "AUS", "2": "EIN", "3": "AUTO"}
    # cmi_hint (Status-Kanon, vorgegeben):
    assert "Druckregelpumpe O8" in o8["cmi_hint"]
    assert config.O8_ADDRESS in o8["cmi_hint"]
    assert "Kein CMI-Write empfohlen" in o8["cmi_hint"]
    # Physik-Text: Druckregelpumpe, Stufung nur am Aggregat.
    assert "Druckregelpumpe" in o8["begruendung_de"]
    assert "physikalischen Aggregat" in o8["begruendung_de"]
    assert "AUS" not in o8["vorgeschlagen"]
    assert o8["tier"] == "B"
    assert o8["zone"] is None


def test_o8_fail_closed_ohne_discovery() -> None:
    """v0.5-rework: ohne gültiges Discovery (``discovery=None``) → Status
    bleibt advisory-manuell (fail-closed), aktueller Modus unbekannt."""
    diag = _diag({"kell": 1.0, "eg": 0.0, "dg": 0.0},
                 {"kell": 5.0, "eg": 40.0, "dg": 40.0})
    adv = tier_b_advisories(_state(), None, [], None, rm_diag=diag)
    o8 = _o8_block(adv)[0]
    # Fail-Closed: NIE cmi-settable (advisory-manuell), kein to_value.
    assert o8["executability"] == config.EXECUTABILITY_ADVISORY_MANUELL
    assert "to_value" not in o8
    # Kein Discovery-Stand → aktueller Modus unbekannt:
    assert o8["aktuell"].startswith("Modus unbekannt")
    assert "kein Discovery-Stand" in o8["aktuell"]
    assert o8["o8"]["aktueller_modus"] is None
    # Statuszeile bleibt (kein Wechsel nötig, manuell am Aggregat):
    assert o8["vorgeschlagen"].startswith("AUTO beibehalten")


# --- Ticket C: VL-Soll-Kalibrierung -----------------------------------------


def make_calib_store(root: Path, n_band12: int, vl_band12: float,
                     n_band9: int = 0, vl_band9: float = 45.0,
                     n_nonheat: int = 0) -> Path:
    """Synthetischer Feature-Store (Hive) für die Kalibrierung:
    - Band 12 (ext 12–15): ``n_band12`` Heizstunden (pump 1,0) mit VL
      ``vl_band12``;
    - Band 9 (ext 9–12): ``n_band9`` Heizstunden;
    - ``n_nonheat`` Nicht-Heizstunden (pump 0,0, VL 65 °C) — müssen durch
      die Heizstunde-Maske ausgeblendet werden.
    """
    rows: list[tuple[str, float, float, float]] = []
    base = datetime(2026, 5, 1)
    for i in range(n_band12):
        t = (base + timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
        rows.append((t, 13.0, vl_band12, 1.0))
    for i in range(n_band9):
        t = (base + timedelta(hours=n_band12 + i)).strftime("%Y-%m-%d %H:00")
        rows.append((t, 10.0, vl_band9, 1.0))
    for i in range(n_nonheat):
        t = (base + timedelta(hours=n_band12 + n_band9 + i)).strftime("%Y-%m-%d %H:00")
        rows.append((t, 13.0, 65.0, 0.0))
    ts = np.array([r[0] for r in rows], dtype="datetime64[h]")
    tbl = pa.table(
        {
            "ts": pa.array(ts.astype("datetime64[us]")),
            "ext": pa.array([r[1] for r in rows], type=pa.float64()),
            "hz_vl": pa.array([r[2] for r in rows], type=pa.float64()),
            "pump_hz_on": pa.array([r[3] for r in rows], type=pa.float64()),
            "oel_burner_on": pa.array([0.0] * len(rows), type=pa.float64()),
            "data_version": pa.array(["m1.3"] * len(rows)),
        }
    )
    p = root / "2026" / "05" / "2026-05.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, p)
    return root


def test_calibrate_vl_baender_und_heizstunde_maske(tmp_path: Any) -> None:
    """DoD-C: Bänder (3 K) + Heizstunde-Maske (pump_hz_on ≥ 0,5) —
    Nicht-Heizstunden (pump 0, VL 65 °C) zählen NICHT; n pro Band korrekt;
    dev gegen UVR-Standard-Heizkurve (``vlst_est``/``HK_CURVE``)."""
    root = make_calib_store(tmp_path / "f", n_band12=60, vl_band12=40.0,
                            n_band9=10, n_nonheat=30)
    art = vl_cal.calibrate_vl(features_dir=root, now="2026-11-07 00:00:00",
                              out_path=tmp_path / "vl_calib.json")
    # Maske: nur die 70 Heizstunden (60 + 10), nicht die 30 Nicht-Heizstunden.
    assert art["n_heizstunden"] == 70
    assert art["window_days"] == config.VL_KALIB_WINDOW_DAYS
    b12 = art["bands"]["12"]
    b9 = art["bands"]["9"]
    # Band 12: n = 60 (Nicht-Heizstunden ausgeblendet → VL-Ø exakt 40,0),
    # usable (n ≥ 50).
    assert b12["n"] == 60
    assert b12["vl_mean"] == pytest.approx(40.0, abs=1e-6)
    assert b12["usable"] is True
    assert b12["band_lo"] == 12 and b12["band_hi"] == 15
    assert b12["ext_mean"] == pytest.approx(13.0, abs=1e-6)
    # dev gegen Standard-Kurve (ext 13 → vlst_est(13)):
    from analysis.model.m2b.features import vlst_est

    curve13 = float(vlst_est(np.array([13.0]))[0])
    assert b12["curve_vl_soll"] == pytest.approx(curve13, abs=1e-6)
    assert b12["dev_k"] == pytest.approx(40.0 - curve13, abs=1e-6)
    # Band 9: n = 10 < min_n (50) → nicht usable.
    assert b9["n"] == 10
    assert b9["usable"] is False
    # Artefakt persistiert + reloadbar (Cache-Pfad).
    assert (tmp_path / "vl_calib.json").exists()
    loaded = vl_cal.load_vl_calibration(tmp_path / "vl_calib.json")
    assert loaded is not None and loaded["bands"]["12"]["n"] == 60


def test_calibrate_vl_fenster_und_pfad() -> None:
    """Fenster (``window_days``, injizierbar) + Artefakt-Pfad (Daten-Root)."""
    assert vl_cal.vl_calibration_path() == config.VL_KALIB_ESTIMATE_PATH
    assert vl_cal.vl_calibration_path(Path("/data")) == Path("/data/l2/vl_kalibrierung.json")
    art = vl_cal.calibrate_vl(
        features_dir=None,
        window_days=5,
        now=datetime(2026, 11, 7, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    assert art["window_days"] == 5


def test_load_vl_calibration_fehlerfaelle(tmp_path: Any) -> None:
    """Cache fehlt/ungültig → None (→ v0.4-Heuristik-Fallback)."""
    assert vl_cal.load_vl_calibration(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ nicht-json")
    assert vl_cal.load_vl_calibration(bad) is None
    noz = tmp_path / "no_bands.json"
    noz.write_text("{}")
    assert vl_cal.load_vl_calibration(noz) is None
    # leer (kein usables Band) → usable_bands = []:
    assert vl_cal.usable_bands(None) == []
    assert vl_cal.usable_bands({"bands": {"9": {"usable": False, "band_lo": 9}}}) == []


def test_band_for_ext_grenzen() -> None:
    """Außen-T-Bänder (3 K, floor; negative T: Band [−3, 0))."""
    assert vl_cal.band_for_ext(10.4) == 9
    assert vl_cal.band_for_ext(9.0) == 9
    assert vl_cal.band_for_ext(12.0) == 12
    assert vl_cal.band_for_ext(-1.0) == -3
    assert vl_cal.band_for_ext(0.0) == 0
    assert vl_cal.band_for_ext(15.0) == 15


def test_raster_sign_grenzen() -> None:
    """Raster-Abweichungs-Direktion (konservativ bei ``VL_RASTER_K`` = 1 K):
    dev > +1 K → −1 (wärmer → Absenken), dev < −1 K → +1 (kälter → Anheben,
    Abweichungs-Direktion), sonst 0. (Zielbild-Politik „kein Anheben“ liegt in
    ``recommend._vl_curve_advice`` — getestet dort.)"""
    assert vl_cal.raster_sign(1.5) == -1
    assert vl_cal.raster_sign(-1.5) == 1
    assert vl_cal.raster_sign(0.8) == 0
    assert vl_cal.raster_sign(-0.5) == 0
    assert vl_cal.raster_sign(0.0) == 0


def test_curve_deviation_cache_vs_fallback(tmp_path: Any) -> None:
    """Pro-Call: usables Band → Band-Deviation (Cache); Band nicht
    kalibriert → Fallback auf Ist-VL vs Standard-Kurve (``band=None``)."""
    root = make_calib_store(tmp_path / "f", n_band12=60, vl_band12=40.0, n_band9=10)
    art = vl_cal.calibrate_vl(features_dir=root, now="2026-11-07 00:00:00",
                              out_path=tmp_path / "vl_calib.json")
    # Band 12 kalibriert (dev = 40 − curve(13)) → Cache-Deviation:
    dev12, band12 = vl_cal.curve_deviation_at(art, 13.0, vl_now=32.0)
    from analysis.model.m2b.features import vlst_est

    assert band12 is not None and band12["band_lo"] == 12
    assert dev12 == pytest.approx(40.0 - float(vlst_est(np.array([13.0]))[0]), abs=1e-6)
    # Band 9 (n=10 < 50) nicht usable → Fallback: VL_now − curve(10):
    dev9, band9 = vl_cal.curve_deviation_at(art, 10.0, vl_now=32.0)
    assert band9 is None
    assert dev9 == pytest.approx(32.0 - float(vlst_est(np.array([10.0]))[0]), abs=1e-6)
    # Kein Artefakt → Fallback:
    dev_none, band_none = vl_cal.curve_deviation_at(None, 13.0, vl_now=32.0)
    assert band_none is None


def _set_store_cols(root: Path, **cols: Any) -> Any:
    """Mini-Store-Spalten überschreiben (Skalare, alle Zeilen)."""
    p = root / "2026" / "11" / "2026-11.parquet"
    t = pq.read_table(p)
    for name, val in cols.items():
        vals = [None if val is None else float(val)] * N_ROWS
        t = t.set_column(
            t.schema.get_field_index(name), pa.field(name, pa.float64()),
            pa.array(vals, type=pa.float64()),
        )
    pq.write_table(t, p)
    return root


def test_vl_kalibrierung_e2e_bewiesener_band(tmp_path: Any, wx_ok: Any,
                                              skill_map: Any,
                                              monkeypatch: Any) -> None:
    """DoD-C (e2e): usables Außen-T-Band (12) im Cache → Kalibrierungs-
    Empfehlung (statt Heuristik): dev aus dem Band, Ziel-VL-Soll =
    Standard-Kurve + Raster; „Kalibrierung nicht verfügbar“ NICHT da.
    Szenario: Band-Ø WÄRMER als Kurve (dev +3,0 K) → Absenken (−1 Raster)."""
    root = make_calib_store(tmp_path / "cal", n_band12=60, vl_band12=46.0, n_band9=10)
    art = vl_cal.calibrate_vl(features_dir=root, now="2026-11-07 00:00:00",
                              out_path=tmp_path / "vl_calib.json")
    # Mini-Store (heize aktiv, ext 13,0 → Band 12 usable):
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            ext=13.0, hz_vl=46.0,
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_calibration", lambda *a, **k: art
    )
    rec = recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                    skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    from analysis.model.m2b.features import vlst_est

    curve13 = float(vlst_est(np.array([13.0]))[0])  # 43,0 (HK_CURVE)
    dev = 46.0 - curve13  # +3,0 K (wärmer) → −1 Raster (absenken)
    assert vl_cal.raster_sign(dev) == -1
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    assert "VL-Kalibrierung (Heizstunden-Only, n=60" in vl["begruendung_de"]
    assert "Kalibrierung nicht verfügbar" not in vl["begruendung_de"]
    ziel = curve13 - config.VL_RASTER_K
    assert f"Ziel-VL-Soll bei Außen 13.0 °C = {ziel:.1f} °C" in vl["begruendung_de"]
    # v0.5-rework: Klartext in den Hauptzeilen + Jargon im detail-Feld.
    assert "Ist-VL 46.0 °C bei Außen 13.0 °C" in vl["aktuell"]
    assert f"VL ~{dev:.1f} K zu hoch" in vl["aktuell"]
    assert "Standard-Kurve" in vl["aktuell"]
    assert "Raster" not in vl["aktuell"]  # Jargon wandert ins Detail
    assert "1 Raster absenken" in vl["vorgeschlagen"]
    assert "~0,5–1 K weniger VL-Soll" in vl["vorgeschlagen"]
    assert "etwas weniger Öl" in vl["vorgeschlagen"]
    assert "ohne Komfortverlust" in vl["vorgeschlagen"]
    assert "Manuell im UVR/TA-Designer" in vl["vorgeschlagen"]
    assert vl.get("detail") == vl["begruendung_de"]


def test_heizkurve_klartext_fallback_und_detail() -> None:
    """v0.5-rework (Ticket C): Cache fehlt → Klartext in
    ``aktuell``/``vorgeschlagen`` („Ist-VL … bei Außen … → VL ~x K zu niedrig“,
    „1 Raster absenken → ~0,5–1 K weniger VL-Soll“), Jargon
    (Raster/Kalibrierung/„Kalibrierung nicht verfügbar“) im ``detail``-Feld
    (= ``begruendung_de``); Raster-Berechnung (v0.4-Heuristik) unverändert
    (dev = Ist-VL − Kurve; dev ≤ −VL_ERR_HIGH_K → −1 Raster, Ziel = Kurve − 1)."""
    from analysis.model.m2b.features import vlst_est

    st = _state(ext=9.0, hz_vl=40.0)  # aktiv heizend (oel_burner_on_sum3h=1,0)
    adv = tier_b_advisories(st, None, [], _real_discovery())
    vl = next(e for e in adv if e["parameter"] == "Heizkurve/VL-Soll")
    curve9 = float(vlst_est(np.array([9.0]))[0])  # ≈ 45,7
    dev = 40.0 - curve9  # ≈ −5,7 K → −1 Raster (Absatz-Heuristik, dev ≤ −2 K)
    # Klartext-Hauptzeilen:
    assert f"Ist-VL 40.0 °C bei Außen 9.0 °C (Standard-Kurve {curve9:.1f} °C" in vl["aktuell"]
    assert f"VL ~{abs(dev):.1f} K zu niedrig" in vl["aktuell"]
    assert "Raster" not in vl["aktuell"]
    assert "1 Raster absenken" in vl["vorgeschlagen"]
    assert "~0,5–1 K weniger VL-Soll (etwas weniger Öl), ohne Komfortverlust" in vl["vorgeschlagen"]
    assert f"Ziel-VL ~{curve9 - config.VL_RASTER_K:.1f} °C" in vl["vorgeschlagen"]
    assert "Manuell im UVR/TA-Designer" in vl["vorgeschlagen"]
    # Detail-Feld (= Begründung, Jargon):
    assert vl["detail"] == vl["begruendung_de"]
    assert "Kalibrierung nicht verfügbar" in vl["detail"]
    assert f"Raster-/Kalibrierung: Abweichung {dev:+.1f} K" in vl["detail"]
    assert f"Ziel-VL-Soll bei Außen 9.0 °C = {curve9 - config.VL_RASTER_K:.1f} °C" in vl["detail"]
    assert "Ölbrenner/Vorlaufsoll selbst werden NIE direkt empfohlen" in vl["detail"]


def test_heizkurve_raster_plus1_fallback_anheben() -> None:
    """v0.5-rework (Coverage: ``raster > 0``-Zweig, Fallback-Heuristik):
    Ist-VL deutlich ÜBER der Kurve (dev ≥ +2 K, kein Kalibrierungs-Band) →
    „+1 Raster anheben → ~0,5–1 K mehr VL-Soll (mehr Wärme)“ (das
    „kein-Anheben“-Zielbild gilt nur mit usables Kalibrierungs-Band,
    s. ``test_vl_kalibrierung_zielbild_kein_anheben``)."""
    from analysis.model.m2b.features import vlst_est

    st = _state(ext=9.0, hz_vl=50.0)  # aktiv heizend
    adv = tier_b_advisories(st, None, [], _real_discovery())
    vl = next(e for e in adv if e["parameter"] == "Heizkurve/VL-Soll")
    curve9 = float(vlst_est(np.array([9.0]))[0])
    dev = 50.0 - curve9  # ≈ +4,3 K → +1 Raster (anheben, Fallback)
    assert f"VL ~{dev:.1f} K zu hoch" in vl["aktuell"]
    assert "1 Raster anheben" in vl["vorgeschlagen"]
    assert "~0,5–1 K mehr VL-Soll (mehr Wärme)" in vl["vorgeschlagen"]
    assert f"Ziel-VL ~{curve9 + config.VL_RASTER_K:.1f} °C" in vl["vorgeschlagen"]
    assert "Regelbasiert: Raster +1" in vl["detail"]


def test_vl_kalibrierung_e2e_fallback_hint(tmp_path: Any, wx_ok: Any,
                                           skill_map: Any) -> None:
    """DoD-C: Cache fehlt (``None``) → v0.4-Heuristik (Ist-VL vs Kurve) +
    Hinweis „Kalibrierung nicht verfügbar“ (Mini-Store: ext 9,0 → Kurve
    44,7 °C; Ist-VL 32,0 → dev −12,7 K → −1 Raster)."""
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Kalibrierung nicht verfügbar" in vl["begruendung_de"]
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    from analysis.model.m2b.features import vlst_est

    curve9 = float(vlst_est(np.array([9.0]))[0])  # ≈ 44,7
    assert f"Ziel-VL-Soll bei Außen 9.0 °C = {curve9 - config.VL_RASTER_K:.1f} °C" \
        in vl["begruendung_de"]


def test_vl_kalibrierung_beibehalten_im_raster(tmp_path: Any, wx_ok: Any,
                                               skill_map: Any,
                                               monkeypatch: Any) -> None:
    """DoD-C: Band-Deviation im Raster (|dev| < 1 K) → „beibehalten“."""
    root = make_calib_store(tmp_path / "cal", n_band12=60, vl_band12=43.5, n_band9=10)
    art = vl_cal.calibrate_vl(features_dir=root, now="2026-11-07 00:00:00",
                              out_path=tmp_path / "vl_calib.json")
    # curve(13) = 43,0; VL-Ø 43,5 → dev +0,5 K → im Raster (beibehalten).
    assert vl_cal.raster_sign(art["bands"]["12"]["dev_k"]) == 0
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            ext=13.0, hz_vl=40.0,
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_calibration", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "beibehalten" in vl["vorgeschlagen"]
    assert "im Raster" in vl["vorgeschlagen"]
    from analysis.model.m2b.features import vlst_est

    curve13 = float(vlst_est(np.array([13.0]))[0])
    assert f"Ziel-VL-Soll bei Außen 13.0 °C = {curve13:.1f} °C" in vl["begruendung_de"]


def test_vl_kalibrierung_zielbild_kein_anheben(tmp_path: Any, wx_ok: Any,
                                               skill_map: Any,
                                               monkeypatch: Any) -> None:
    """DoD-C (Zielbild „VL so niedrig wie möglich“): kältere Abweichung
    (Band-Ø < Kurve, dev < −VL_RASTER_K) → Kurve **NICHT anheben** (Komfort
    via RAS-Offsets; kein data-belegtes weiteres Absenken) → „beibehalten“,
    Ziel = Standard-Kurve. (Fallback-Heuristik ohne Kalibrierung hebt an,
    s. ``test_v004_*``-Regression — hier aber kalibriert.)"""
    root = make_calib_store(tmp_path / "cal", n_band12=60, vl_band12=38.0, n_band9=10)
    art = vl_cal.calibrate_vl(features_dir=root, now="2026-11-07 00:00:00",
                              out_path=tmp_path / "vl_calib.json")
    # curve(13) = 43,0; VL-Ø 38,0 → dev −5,0 K (kälter) → kein Anheben.
    assert art["bands"]["12"]["dev_k"] == pytest.approx(-5.0, abs=0.1)
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            ext=13.0, hz_vl=40.0,
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_calibration", lambda *a, **k: art
    )
    rec = recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                    skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "+1 Raster" not in vl["vorgeschlagen"]
    assert "beibehalten" in vl["vorgeschlagen"]
    assert "kein Anheben" in vl["begruendung_de"]
    from analysis.model.m2b.features import vlst_est

    curve13 = float(vlst_est(np.array([13.0]))[0])
    assert f"Ziel-VL-Soll bei Außen 13.0 °C = {curve13:.1f} °C" in vl["begruendung_de"]


# --- JSON-Format-Regression (A/B/C im Gesamt-JSON) ---------------------------


def test_json_format_regression_abc_blocks(tmp_path: Any, wx_ok: Any,
                                           skill_map: Any,
                                           monkeypatch: Any) -> None:
    """DoD-A/B/C: JSON-Format-Regression — neue Blocks tragen alle
    Schema-Felder; Minderer/O8-Modus tragen KEIN ``adresse``/``to_value``
    (O8 nur als ``o8``-Objekt, UI-Gate via cmi-settable + S3-1);
    Determinismus (zwei Läufe identisch)."""
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    j = rec.to_json()
    # Kondition aktiv (Mini-Store: alle Minderer offen 1,0) → A- + B-Blocks:
    assert j["rm_diagnostics"]["active"] is True
    params = {e["parameter"] for e in j["empfehlungen"]}
    for z in config.ZONES:
        assert f"Minderer-Feinjustierung {z}" in params
    assert "Pumpe Heizkreis O8 (Modus)" in params
    for e in j["empfehlungen"]:
        for key in ("parameter", "aktuell", "vorgeschlagen", "wirkung_prognose",
                    "begruendung_de", "konfidenz", "executability", "cmi_hint",
                    "tier"):
            assert key in e, key
        if e["parameter"] != "Pumpe Heizkreis O8 (Modus)":
            assert "adresse" not in e
            assert "to_value" not in e
    # Determinismus:
    rec2 = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                             skill_map=skill_map)
    assert rec2.to_json() == j


# --- RAS-Offset De-Emphasis (v0.5-engine-rework) ------------------------------


def test_ras_offset_deemphasis_raum_text(tmp_path: Any, wx_ok: Any,
                                         skill_map: Any) -> None:
    """v0.5-rework: Tier-A-Blocks tragen ``raum_text`` (maintainer-Beleg-Format,
    prominent in der UI) + Handlung-Einzeiler „Raum-Soll bei Bedarf direkt
    im UVR/TA-Designer setzen (Offset intern)“; state-Präfix
    „Ist … (Soll …, Abw. …)“ bleibt; ``aktuell``/``vorgeschlagen`` im
    Offset-K-Format (gepinnt, unverändert)."""
    from analysis.model.l2 import recommend as rec_mod

    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    tier_a = [e for e in rec.empfehlungen if e["tier"] == "A"]
    assert len(tier_a) == len(config.ZONES)
    for e in tier_a:
        z = e["zone"]
        assert e["raum_text"].startswith(f"Raum {z}: ")
        assert "°C (Ist) vs" in e["raum_text"]
        assert "°C (Soll; Abw." in e["raum_text"]
        # state-Präfix bleibt in der Begründung (gepinnt):
        assert e["begruendung_de"].startswith("Ist ")
        assert "(Soll " in e["begruendung_de"] and "Abw. " in e["begruendung_de"]
        # Handlung-Einzeiler:
        assert "Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen (Offset intern)" \
            in e["begruendung_de"]
        # Lange Kosten-/Optimierungs-Erklärung ist weg:
        assert "keine kostengünstigere" not in e["begruendung_de"]
        assert "reduziert die 12-h-Band-Überschreitung" not in e["begruendung_de"]
        # Offset-K-Format (gepinnt):
        assert e["aktuell"].endswith(" K") and e["vorgeschlagen"].endswith(" K")
        if e["changed"]:
            assert "Advisory (Offset intern, stündliche Feinjustierung)" in e["begruendung_de"]
    # Keller (Ist 16,5 @ off +3,5 → Soll 16,5, Abw. +0,0 K):
    kell = next(e for e in tier_a if e["zone"] == "kell")
    assert "Ist 16.5 °C (Soll 16.5 °C, Abw. +0.0 K)" in kell["begruendung_de"]
    assert "16.5 °C (Ist) vs 16.5 °C (Soll; Abw. +0.0 K)" in kell["raum_text"]
