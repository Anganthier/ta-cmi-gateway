"""L2 v0.3 Fix-2 — Effektor-Guard: Rücklaufminderer 3-h-Fenster (Offset-Stellkraft).

Regression-Tests für die maintainer-Entscheidung 2026-11-03
(docs/l2-v0.3-ist-kalibrierung.md §„Effektor-Guard“): der RAS kann heizen,
aber NICHT kühlen → ist der Rücklaufminderer seit ~3 h zu (perzipiert >
20,1 °C, Regelkreis aus, Raum von externer Wärmequelle z. B. Trocknungsgerät
gehalten), hat der Offset **keine Stellkraft** („bloß“): Δ_z = 0 erzwingen
(No-Change je Zone) + Text „Rücklaufminderer zu seit 3 h …“. Flappend +
Raum außerhalb des Komfort-Bands → Advisory-Hinweis (Heizkurve Q-108 /
Minderer-Einstellung) im Text, KEINE Kandidaten-Untersagung. NULL in einer
der 3 Stunden → „unbekannt“ → Guard stumm (Fail-Open).

Signal: ``hzrkl_{z}_rule`` (Offen-Anteil: 1 = offen/perz < 20,1,
0 = zu/perz > 20,1) + ``_lag1``/``_lag2`` → 3-h-Fenster aus EINER
Store-Zeile (kein extra Ladepfad). Synthetic-Store + FakePredictor,
kein Live-Netz, kein echtes L1-Artefakt.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from analysis.model.l2.recommend import generate_candidates, minderer_state, recommend
from analysis.model.l2.tests.conftest import N_ROWS, make_mini_store
from analysis.model.l2.tests.test_v003_ist_kalibrierung import (
    OFF,
    TS,
    FakePredictorV3,
    _set_cols,
)


def _set_minderer(
    root: Any, kell: Any = None, eg: Any = None, dg: Any = None
) -> Any:
    """Minderer-3-h-Werte je Zone setzen: Spec = (rule, lag1, lag2) — je ein
    Wert (Skalar → alle N_ROWS, oder Array) pro SPALTEN-Position.

    Eine Store-Zeile trägt das ganze Fenster: ``rule`` = Offen-Anteil der
    Stunde t, ``lag1`` = t−1, ``lag2`` = t−2 (m1.3-Lags). ``None`` (als
    Skalar oder Array-Element) = NULL → Zustand „unbekannt“.
    """
    p = root / "2026" / "11" / "2026-11.parquet"
    t = pq.read_table(p)
    for z, spec in (("kell", kell), ("eg", eg), ("dg", dg)):
        if spec is None:
            continue
        for lag, val in zip(("", "_lag1", "_lag2"), spec, strict=False):
            seq = list(val) if isinstance(val, (list, tuple, np.ndarray)) else [val] * N_ROWS
            if len(seq) < N_ROWS:
                seq = list(seq) + [seq[-1]] * (N_ROWS - len(seq))
            vals = [None if x is None else float(x) for x in seq[:N_ROWS]]
            col = f"hzrkl_{z}_rule{lag}"
            t = t.set_column(
                t.schema.get_field_index(col), pa.field(col, pa.float64()),
                pa.array(vals, type=pa.float64()),
            )
    pq.write_table(t, p)
    return root


def _delta(key: str, zone: str) -> float:
    """Zone-Komponente eines Kandidaten-Schlüssels (z. B. 'kell:+0.5+…')."""
    return float(re.search(rf"{zone}:([+-]?\d+\.\d)", key).group(1))


def _kell_closed_store(root: Any) -> Any:
    """Keller zu-warm + Minderer seit 3 h zu (rule/lag1/lag2 = 0):
    perz 25,2 haltend, Ist 21,7 @ off +3,5 → Intent 16,5 (+5,2 K)."""
    _set_cols(root, ras_kell_ist=21.7, ras_kell_perc=25.2, ras_kell_perc_last=25.2)
    return _set_minderer(root, kell=[0.0, 0.0, 0.0])


def test_minderer_state_klassifikation() -> None:
    """3-h-Ø(open) → Status: ≤0,1 „zu“, ≥0,9 „offen“, sonst „flappend“;
    NULL/fehlend in einer der 3 Stunden → (None, „unbekannt“)."""
    def flat(v):
        return {f"hzrkl_kell_rule{lag}": v for lag in ("", "_lag1", "_lag2")}

    assert minderer_state(flat(0.0), "kell") == (0.0, "zu")
    off, st = minderer_state(flat(0.05), "kell")
    assert st == "zu" and abs(off - 0.05) < 1e-12
    assert minderer_state(flat(1.0), "kell") == (1.0, "offen")
    off, st = minderer_state(flat(0.2), "kell")
    assert st == "flapping" or st == "flappend"
    assert abs(off - 0.2) < 1e-12
    # NULL in JEDER der 3 Stunden → unbekannt (Fail-Open).
    rows = flat(1.0)
    rows["hzrkl_eg_rule_lag1"] = None
    assert minderer_state(rows, "eg") == (None, "unbekannt")
    rows2 = flat(0.0)
    rows2["hzrkl_dg_rule"] = None
    assert minderer_state(rows2, "dg") == (None, "unbekannt")
    assert minderer_state({}, "kell") == (None, "unbekannt")


def test_keller_minderer_zu_unterdrueckt(
    tmp_path: Any, wx_ok: Any, skill_map: Any
) -> None:
    """Keller [0,0,0] (zu) + zu-warm: kein Kandidat mit Δ_kell ≠ 0 wird
    gewählt; No-Change; „vorgeschlagen“ = „aktuell“; Grundtext enthält
    „Rücklaufminderer zu seit 3 h“ (Stellkraft fehlt)."""
    root = _kell_closed_store(make_mini_store(tmp_path / "mind_zu"))
    p = FakePredictorV3(
        perz_base={"kell": 25.2, "eg": 20.0, "dg": 20.0},
        gain={"kell": 1.0, "eg": 0.0, "dg": 0.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    kell = by_param["RAS-Offset kell"]
    assert kell["aktuell"] == "+3.5 K"
    assert kell["vorgeschlagen"] == kell["aktuell"]
    assert kell["changed"] is False
    assert "keine Änderung" in kell["wirkung_prognose"]
    assert "Rücklaufminderer zu seit 3 h" in kell["begruendung_de"]
    assert "keine Stellkraft" in kell["begruendung_de"]
    assert "Trocknungsgerät" in kell["begruendung_de"]
    # (a) Kein Keller-Δ≠0-Kandidat in der Kosten-Abbildung (Δ_kell = 0 erzwungen).
    costs = {row["kandidat"] for row in rec.kosten["kandidaten"]}
    assert costs, "Kandidaten-Leerbestand"
    for key in costs:
        assert _delta(key, "kell") == 0.0, key
    # (b) No-Change = beste Kandidatin (EG/DG im Gleichgewicht → nichts günstiger).
    assert rec.kosten["beste"]["kandidat"] == "kell:+0.0+eg:+0.0+dg:+0.0"
    # (c) Filter ist in den Guardrail-Hits deterministisch dokumentiert.
    assert any("minderer kell" in h for h in rec.guardrail_hits)
    # (d) EG/DG (offen) behalten ihre normalen Texte (keine Guard-Artefakte).
    for z in ("eg", "dg"):
        assert "Rücklaufminderer" not in by_param[f"RAS-Offset {z}"]["begruendung_de"]


def test_flap_hint_outside_band(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """Flap-Hinweis: DG rule [1,0,0] (3-h-Ø ≈ 0,33 = flappend) + Raum 3 K
    unter dem Intent (perz 17,0 haltend, off +1,4 → Ist 15,6, Intent 18,6,
    dev −3,0) → Hinweis „Heizkreis aktiviert periodisch …“ (Q-108) im Text;
    Offset-Optimierung läuft weiter (Kandidaten mit Δ ≠ 0 erlaubt)."""
    root = make_mini_store(tmp_path / "mind_flap")
    _set_cols(root, ras_dg_ist=15.6, ras_dg_perc=17.0, ras_dg_perc_last=17.0)
    root = _set_minderer(root, dg=(1.0, 0.0, 0.0))  # rule(t)=1, lag1(t−1)=0, lag2(t−2)=0
    p = FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.0, "dg": 17.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 1.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    dg = by_param["RAS-Offset dg"]
    assert "Heizkreis aktiviert periodisch" in dg["begruendung_de"]
    assert "Q-108" in dg["begruendung_de"]
    # KEINE Kandidaten-Untersagung: DG-Δ≠0 bleibt im Kosten-Abbild.
    costs = {row["kandidat"] for row in rec.kosten["kandidaten"]}
    assert any(_delta(k, "dg") != 0.0 for k in costs), "Optimierung blockiert?"
    # Offene Zonen (Keller/EG, rule [1,1,1]) bleiben ohne Hinweis.
    for z in ("kell", "eg"):
        assert "periodisch" not in by_param[f"RAS-Offset {z}"]["begruendung_de"]


def test_eg_false_positive_no_hint(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """EG-False-Positiv-Absicherung: rule [1,0,0] (flappend) aber ON-Intent
    (Ist 21,8, off −1,7 → Intent 21,7, Abw. +0,1 < Band 0,5) → KEIN
    Heizkurve-Hinweis; Normalpfad (No-Change)."""
    root = make_mini_store(tmp_path / "mind_fp")
    _set_cols(
        root,
        offset_eg=-1.7, ras_eg_ist=21.8, ras_eg_perc=20.1, ras_eg_perc_last=20.1,
    )
    root = _set_minderer(root, eg=(1.0, 0.0, 0.0))
    p = FakePredictorV3(
        perz_base={"kell": 20.0, "eg": 20.1, "dg": 20.0},
        gain={"kell": 0.0, "eg": 0.0, "dg": 0.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    eg = by_param["RAS-Offset eg"]
    assert "Heizkreis aktiviert periodisch" not in eg["begruendung_de"]
    assert "Q-108" not in eg["begruendung_de"]
    assert "Rücklaufminderer zu" not in eg["begruendung_de"]
    # Normalpfad: No-Change (Abw. +0,1 im Band; Δ teurer über Change-Strafe).
    assert eg["changed"] is False
    assert eg["aktuell"] == eg["vorgeschlagen"]
    assert rec.kosten["beste"]["kandidat"] == "kell:+0.0+eg:+0.0+dg:+0.0"


def test_null_fail_open(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """NULL-Fail-Open: Minderer-Werte NULL → Zustand „unbekannt“ → kein
    Filter (Kandidaten mit Δ_kell ≠ 0 unverändert im Kosten-Abbild), kein
    Guard-Text, kein Guardrail-Hit."""
    root = _kell_closed_store(make_mini_store(tmp_path / "mind_null"))
    root = _set_minderer(root, kell=[None, None, None])
    p = FakePredictorV3(
        perz_base={"kell": 25.2, "eg": 20.0, "dg": 20.0},
        gain={"kell": 1.0, "eg": 0.0, "dg": 0.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    kell = by_param["RAS-Offset kell"]
    assert "Rücklaufminderer" not in kell["begruendung_de"]
    assert not any("minderer" in h for h in rec.guardrail_hits)
    costs = {row["kandidat"] for row in rec.kosten["kandidaten"]}
    assert any(_delta(k, "kell") != 0.0 for k in costs), "Filter aktiv trotz NULL?"


def test_offen_guard_stumm(tmp_path: Any, wx_ok: Any, skill_map: Any) -> None:
    """Offen (Normal): rule [1,1,1] (3-h-Ø 1,0) → Guard stumm = exakt das
    Fix-1-Verhalten (volle Kandidatenmenge, keine Guard-Texte/Hits)."""
    root = _kell_closed_store(make_mini_store(tmp_path / "mind_offen"))
    root = _set_minderer(root, kell=[1.0, 1.0, 1.0])
    p = FakePredictorV3(
        perz_base={"kell": 25.2, "eg": 20.0, "dg": 20.0},
        gain={"kell": 1.0, "eg": 0.0, "dg": 0.0},
    )
    rec = recommend(TS, features_dir=root, wx=wx_ok, predictor=p, skill_map=skill_map)
    assert rec.stale is False
    by_param = {e["parameter"]: e for e in rec.empfehlungen if e["tier"] == "A"}
    kell = by_param["RAS-Offset kell"]
    assert "Rücklaufminderer" not in kell["begruendung_de"]
    assert "periodisch" not in kell["begruendung_de"]
    assert not any("minderer" in h for h in rec.guardrail_hits)
    costs = {row["kandidat"] for row in rec.kosten["kandidaten"]}
    assert any(_delta(k, "kell") != 0.0 for k in costs)
    # Volle (gecappte) Kandidatenmenge wie ohne Guard (Fix-1-Verhalten).
    assert rec.n_candidates == len(generate_candidates(dict(OFF)))
