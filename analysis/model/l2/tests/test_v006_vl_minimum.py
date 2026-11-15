"""L2 v0.6-B — L2-Heizkurve v2: „VL-Minimum s.t. Räume am Soll“ + Recover.

Tests für :mod:`analysis.model.l2.vl_minimum` (Cache-Bau + -Lesung) und die
Heizkurven-Kaskade in :func:`analysis.model.l2.recommend._vl_curve_advice`
(Recover → datenbasiert → v0.5-Fallback):

- **Cache-Bau** (``calibrate_vl_min``): Heizstunde-Maske
  (``pump_hz_on ≥ VL_KALIB_HEAT_EPS``) + **Lernstart-Maske**
  (``ts ≥ VL_MIN_LEARN_START`` = 2026-11-07 16:14) + Am-Soll-Kriterium
  (alle geloggten Zonen: ``ras_z_ist − (RAS_BASE_C − offset_z)`` ≥ −1,5 K) +
  95.-Perzentil der ``hz_vl`` + ``usable``/``konfidenz`` (``n_am_soll ≥
  VL_MIN_MIN_N``) + Bänder (3 K, konsistent zu ``vl_calibration``).
- **Cache-Read** (``load_vl_minimum``/``band_at``): fehlt/ungültig →
  ``None`` (→ Kaskade überspringt Stufe 2, v0.5-Verhalten exakt);
  Band usable + ``vl_min_soll`` vorhanden → Band-Objekt.
- **Recover** (Kaskade-Stufe 1, live ohne Cache): Raum ≥ 1,5 K unter Soll →
  „Kurven-Absatz +N Raster (temporär, bis Raum am Soll, dann zurück)“,
  ``N = min(2, ceil((|dev| − 1,5 K)/VL_RASTER_K))``; Klartext-Hinweis
  UVR610-reaktiv (nicht doppelt gezählt); OVERRIDE nur bei
  ``dev ≤ −1,5 K`` (andernfalls Kaskade-2/3).
- **Datenbasiert** (Kaskade-Stufe 2): usables VL-Minimum-Band → Empfehlung
  gegen das Minimum (statt Standardkurve): unter → +1 Raster (anheben),
  > 3 Raster darüber → −1 Raster (absenken), dazwischen → beibehalten;
  Klartext „Datenbasiert (n=X Heizstunden ab DD.MM.: …)“ (Jargon im
  ``detail``-Feld).
- **Fallback** (Kaskade-Stufe 3): Datenmangel (kein VL-Minimum-Cache) →
  exakt v0.5 (Regression: v0.4-Guard + v0.5-Pins bleiben grün, hier mit
  Mini-Store + FakePredictor nachvollzogen).

Synthetische Stores/Artefakte, kein Live-Netz, kein CMI-Zugriff,
deterministisch (kein RNG).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.l2 import config
from analysis.model.l2 import vl_minimum as vl_min
from analysis.model.l2.recommend import tier_b_advisories
from analysis.model.l2.tests.conftest import make_mini_store
from analysis.model.l2.tests.test_v003_ist_kalibrierung import TS
from analysis.model.l2.tests.test_v005_tickets_abc import (
    _p_eq,
    _set_store_cols,
    _state,
)

# --- Synthetischer VL-Minimum-Store (Hive, m1.3-Namen) ----------------------


def make_vlmin_store(
    root: Path,
    n_pre_learn: int = 5,
    n_am_soll: int = 60,
    n_nicht: int = 10,
    vl_am_soll: float = 40.0,
    ext: float = 13.0,
) -> Path:
    """Synthetischer Store für ``calibrate_vl_min`` (ein Außen-T-Band).

    - ``n_pre_learn``: Heizstunden im Oktober (vor dem Lernstart
      2026-11-07 16:14) — zählen NICHT (Lernstart-Maske).
    - ``n_am_soll``: Heizstunden im Dezember (im Lernstart-Fenster), in
      denen alle Zonen am Soll sind (``ras_z_ist`` = RAS-Soll = 20 −
      offset) → Am-Soll-Stunden mit ``hz_vl`` = ``vl_am_soll``.
    - ``n_nicht``: Heizstunden im Dezember, in denen DG 2,0 K unter Soll
      liegt (Abw. −2,0 K < −1,5 K) → keine Am-Soll-Stunde.
    """

    def month_table(mm: str, n: int, dg_values: list[float]) -> pa.Table:
        # Stündliche Buckets über mehrere Dezember-Dezember-Tage (n kann
        # > 31 sein): Tag = i // 24 + 1, Stunde = i % 24.
        ts = np.array(
            [f"2026-{mm}-{(i // 24) + 1:02d} {i % 24:02d}:00" for i in range(n)],
            dtype="datetime64[h]",
        )
        return pa.table({
            "ts": pa.array(ts.astype("datetime64[us]")),
            "data_version": pa.array(["m1.3"] * n),
            "ext": pa.array([ext] * n, type=pa.float64()),
            "pump_hz_on": pa.array([1.0] * n, type=pa.float64()),
            "oel_burner_on": pa.array([1.0] * n, type=pa.float64()),
            "hz_vl": pa.array([vl_am_soll] * n, type=pa.float64()),
            "offset_kell": pa.array([0.0] * n, type=pa.float64()),
            "offset_eg": pa.array([0.0] * n, type=pa.float64()),
            "offset_dg": pa.array([0.0] * n, type=pa.float64()),
            # Am-Soll: RAS-Ist = RAS-Soll (20,0) je Zone.
            "ras_kell_ist": pa.array([20.0] * n, type=pa.float64()),
            "ras_eg_ist": pa.array([20.0] * n, type=pa.float64()),
            "ras_dg_ist": pa.array(dg_values, type=pa.float64()),
        })

    # Oktober: vor Lernstart (alle Zonen am Soll — irrelevant, Maske blendet
    # aus; DG = 18,0 wäre hier ebenso möglich).
    p10 = root / "2026" / "10" / "2026-10.parquet"
    p10.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        month_table("10", n_pre_learn, [20.0] * n_pre_learn),
        p10,
    )
    # Dezember: im Fenster; erste n_am_soll Stunden am Soll (DG = 20,0),
    # Rest mit DG-2,0-K-Defizit.
    p12 = root / "2026" / "12" / "2026-12.parquet"
    p12.parent.mkdir(parents=True, exist_ok=True)
    dg_vals = [20.0] * n_am_soll + [18.0] * n_nicht
    pq.write_table(
        month_table("12", n_am_soll + n_nicht, dg_vals),
        p12,
    )
    return root


# --- Cache-Bau (``calibrate_vl_min``) --------------------------------------


def test_calibrate_vl_min_masken_und_baender(tmp_path: Any) -> None:
    """Lernstart-Maske + Heizstunde-Maske + Am-Soll-Kriterium + Bänder
    (3 K) + 95.-Perzentil + ``usable``/``konfidenz``."""
    root = make_vlmin_store(tmp_path / "f", n_pre_learn=5, n_am_soll=60,
                           n_nicht=10, vl_am_soll=40.0, ext=13.0)
    art = vl_min.calibrate_vl_min(features_dir=root, now="2026-11-12 09:00:00",
                                  out_path=tmp_path / "vl_min.json")
    # Lernstart-Maske: nur 70 der 75 Heizstunden zählen (5 vor dem 07.11.
    # 16:14 = nicht belastbar, s. v0.6-Markierung).
    assert art["n_heizstunden"] == 70
    assert art["learn_start"] == config.VL_MIN_LEARN_START
    assert art["quantile"] == 95
    assert art["am_soll_tol_k"] == 1.5
    b = art["bands"]["12"]  # ext 13,0 → Band [12, 15)
    assert b["band_lo"] == 12 and b["band_hi"] == 15
    assert b["ext_mean"] == pytest.approx(13.0, abs=1e-6)
    assert b["n_heizstunden"] == 70
    # Am-Soll-Kriterium: 60 von 70 Stunden (DG-2-K-Defizit-Stunden +
    # pre-Learn-Stunden ausgenommen → 10 Nicht-Am-Soll).
    assert b["n_am_soll"] == 60
    # 95.-Perzentil aus 60 identischen VL-Werten = der Wert.
    assert b["vl_min_soll"] == pytest.approx(40.0, abs=1e-6)
    # usable (n_am_soll = 60 ≥ VL_MIN_MIN_N = 50) + Konfidenz
    # min(1, 60/(2·50)) = 0,6.
    assert b["usable"] is True
    assert b["konfidenz"] == pytest.approx(0.6, abs=1e-9)
    # Artefakt persistiert + reloadbar (Cache-Pfad).
    assert (tmp_path / "vl_min.json").exists()
    loaded = vl_min.load_vl_minimum(tmp_path / "vl_min.json")
    assert loaded is not None and loaded["bands"]["12"]["vl_min_soll"] == 40.0


def test_calibrate_vl_min_mins_n_nicht_usable(tmp_path: Any) -> None:
    """``n_am_soll < VL_MIN_MIN_N`` → ``usable=False`` + ``konfidenz=0``
    (→ Fallback-Verhalten in der Kaskade)."""
    root = make_vlmin_store(tmp_path / "f", n_pre_learn=0, n_am_soll=40,
                           n_nicht=0, vl_am_soll=42.0, ext=6.0)
    art = vl_min.calibrate_vl_min(features_dir=root, now="2026-11-12 09:00:00",
                                  out_path=tmp_path / "vl_min.json")
    b = art["bands"]["6"]
    assert b["n_am_soll"] == 40
    assert b["usable"] is False
    assert b["konfidenz"] == 0.0
    # vl_min_soll bleibt trotzdem berechnet (Beleg für den Report):
    assert b["vl_min_soll"] == pytest.approx(42.0, abs=1e-6)


def test_vl_min_pfade(tmp_path: Any) -> None:
    """Cache-Pfad (Repo-Default + Daten-Root, Muster ``vl_calibration_path``)."""
    assert vl_min.vl_minimum_path() == config.VL_MIN_ESTIMATE_PATH
    assert vl_min.vl_minimum_path(Path("/data")) == Path("/data/l2/vl_minimum.json")


def test_load_vl_min_fehlerfaelle(tmp_path: Any) -> None:
    """Cache fehlt/ungültig → ``None`` (→ Kaskade überspringt Stufe 2)."""
    assert vl_min.load_vl_minimum(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ nicht-json")
    assert vl_min.load_vl_minimum(bad) is None
    noz = tmp_path / "no_bands.json"
    noz.write_text("{}")
    assert vl_min.load_vl_minimum(noz) is None


# --- Cache-Read (``band_at``) -----------------------------------------------


def _min_art(band_lo: int, vl_min_soll: float, n_am_soll: int = 60,
             usable: bool = True) -> dict[str, Any]:
    """Synthetisches VL-Minimum-Artefakt (ein Band)."""
    band_of = int(config.VL_KALIB_BAND_K)
    return {
        "generated_at": "2026-11-12 09:00:00",
        "learn_start": config.VL_MIN_LEARN_START,
        "quantile": 95,
        "am_soll_tol_k": 1.5,
        "min_n_per_band": int(config.VL_MIN_MIN_N),
        "band_k": band_of,
        "n_heizstunden": 100,
        "bands": {
            str(band_lo): {
                "band_lo": band_lo,
                "band_hi": band_lo + band_of,
                "ext_mean": float(band_lo) + 1.5,
                "n_heizstunden": 100,
                "n_am_soll": n_am_soll,
                "vl_min_soll": vl_min_soll,
                "usable": usable,
                "konfidenz": 0.6,
            }
        },
    }


def test_band_at_verwendung() -> None:
    """Usables Band mit ``vl_min_soll`` → Band-Objekt; nicht usable /
    fehlend / ``None``-Artefakt → ``None`` (→ Kaskade-Fallback)."""
    art = _min_art(12, 40.0)
    assert vl_min.band_at(art, 13.0)["vl_min_soll"] == 40.0
    assert vl_min.band_at(art, 12.0)["band_lo"] == 12
    assert vl_min.band_at(_min_art(12, 40.0, usable=False), 13.0) is None
    assert vl_min.band_at(_min_art(6, 40.0), 13.0) is None  # anderes Band
    assert vl_min.band_at(None, 13.0) is None


def test_recover_n_raster_grenzen() -> None:
    """Recover-Raster (Klemme ≥ 1 an der Trigger-Grenze, Kappe 2):
    N = min(2, ceil((|dev| − 1,5 K)/1,0 K))."""
    tol = config.VL_MIN_RECOVER_GAP_K
    assert vl_min.recover_n_raster(-tol) == 1  # exakt an der Grenze → 1
    assert vl_min.recover_n_raster(-2.0) == 1  # ceil(0,5) = 1
    assert vl_min.recover_n_raster(-2.5) == 1  # ceil(1,0) = 1
    assert vl_min.recover_n_raster(-3.5) == 2  # ceil(2,0) = 2
    assert vl_min.recover_n_raster(-4.5) == 2  # ceil(3,0) = 3 → Klemme 2
    assert vl_min.recover_n_raster(-10.0) == 2  # Kappe
    assert vl_min.LEARN_START_SHORT == "07.11."


# --- Kaskade: Recover (Stufe 1, live) ---------------------------------------


def test_recover_e2e_raster1(tmp_path: Any, wx_ok: Any,
                             skill_map: Any, monkeypatch: Any) -> None:
    """Recover-Trigger (Raum 2,0 K unter Soll) → „Kurven-Absatz +1 Raster
    (temporär, bis Raum am Soll, dann zurück)“ + UVR610-Klartext; kein
    Daten-Jargon in ``vorgeschlagen`` (Jargon → ``detail``).

    Mini-Store: kell = 14,5 K gegen Soll 16,5 K (Default-Offset +3,5;
    EG/DG im Soll)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features", kell_ist=14.5),
        ext=9.0,
        hz_vl=32.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    art = _min_art(9, 35.0)  # usables Band (würde Stufe 2 treffen, wenn
    # Recover nicht zuerst greift → Kaskaden-Reihenfolge belegt).
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert vl["vorgeschlagen"].startswith("Kurven-Absatz +1 Raster (temporär")
    assert "Raum kell 2.0 K unter Soll" in vl["vorgeschlagen"]
    assert "bisher" not in vl["vorgeschlagen"]
    assert "Manuell im UVR/TA-Designer" not in vl["vorgeschlagen"]
    assert "Datenbasiert" not in vl["vorgeschlagen"]  # Recover hat OVERRIDE
    # Klartext: UVR610-reaktiv + nicht doppelt gezählt (im Detail):
    assert "UVR610" in vl["detail"]
    assert "nicht doppelt" in vl["detail"]
    assert "Recover (live, kein Cache)" in vl["detail"]
    # aktueller Zustand (Ist-VL bei Außen-T + Raum-Abweichung):
    assert "Ist-VL 32.0 °C bei Außen 9.0 °C" in vl["aktuell"]
    assert "Raum kell -2.0 K unter Soll" in vl["aktuell"]
    assert vl.get("detail") == vl["begruendung_de"]


def test_recover_e2e_raster2_klemme(tmp_path: Any, wx_ok: Any,
                                    skill_map: Any) -> None:
    """Großes Defizit (3,0 K unter Soll) → +2 Raster (Klemme 2)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features", kell_ist=13.5),
        ext=9.0,
        hz_vl=32.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert vl["vorgeschlagen"].startswith("Kurven-Absatz +2 Raster (temporär")
    assert "Raum kell 3.0 K unter Soll" in vl["vorgeschlagen"]


def test_recover_nicht_getriggert_dev_minus_1_k(tmp_path: Any,
                                                wx_ok: Any,
                                                skill_map: Any) -> None:
    """Defizit < 1,5 K (−1,0 K) → kein Recover-OVERRIDE (Kaskade-3:
    v0.5-Fallback bleibt, Mini-Store ohne VL-Minimum-Cache)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features", kell_ist=15.5),
        ext=9.0,
        hz_vl=32.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Kurven-Absatz" not in vl["vorgeschlagen"]
    # v0.5-Heuristik: dev = 32,0 − Kurve(9,0) ≤ −2 K → −1 Raster (Absatz).
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")


def test_recover_unit_tier_b() -> None:
    """Unit: Recover-Zweig via ``tier_b_advisories`` (State-Direkt);
    Raum-Abweichung aus der State-Zeile (v0.3-Ableitung), N = 1."""
    st = _state(
        ext=13.0,
        hz_vl=40.0,
        offset_kell=3.5,
        offset_eg=0.3,
        offset_dg=0.4,
        ras_kell_ist=15.0,  # Soll 16,5 → −1,5 K (Trigger-Grenze)
        ras_eg_ist=21.8,  # Soll 19,7 → +2,1 K
        ras_dg_ist=19.1,  # Soll 19,6 → +0,5 K
    )
    from analysis.model.l2.executability import load_discovery

    disc = load_discovery(config.INVENTAR_DEFAULT_PATH)
    adv = tier_b_advisories(st, None, [], disc, vl_min_art=None)
    vl = next(e for e in adv if e["parameter"] == "Heizkurve/VL-Soll")
    assert vl["vorgeschlagen"].startswith("Kurven-Absatz +1 Raster (temporär")
    assert "Raum kell 1.5 K unter Soll" in vl["vorgeschlagen"]


def test_recover_worst_zone_waehlt() -> None:
    """Min über aktiven Zonen: zwei Räume unter Toleranz → der schlechteste
    wird genannt (defizitgrößen-basiert)."""
    st = _state(
        ext=13.0,
        hz_vl=40.0,
        offset_kell=0.0,
        offset_eg=0.0,
        offset_dg=2.0,
        ras_kell_ist=18.0,  # −2,0 K
        ras_eg_ist=17.0,  # −3,0 K (schlechteste)
        ras_dg_ist=19.1,  # Soll 18,0 → +1,1 K
    )
    from analysis.model.l2.executability import load_discovery

    disc = load_discovery(config.INVENTAR_DEFAULT_PATH)
    adv = tier_b_advisories(st, None, [], disc, vl_min_art=None)
    vl = next(e for e in adv if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Raum eg 3.0 K unter Soll" in vl["vorgeschlagen"]
    assert "+2 Raster" in vl["vorgeschlagen"]


# --- Kaskade: datenbasiert (Stufe 2) ----------------------------------------


def test_datenbasiert_e2e_absenken(tmp_path: Any, wx_ok: Any, skill_map: Any,
                                   monkeypatch: Any) -> None:
    """Ist-VL deutlich über dem VL-Minimum (> 3 Raster) → −1 Raster
    (absenken) + „Datenbasiert“-Klartext (statt Standardkurve-Heuristik).

    Band [9, 12): ext 10,0; VL-Minimum 30,0 °C (n_am_soll 60); Ist-VL
    35,0 → gap +5,0 K → −1 Raster (Ziel 29,0 °C)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features"),
        ext=10.0,
        hz_vl=35.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    art = _min_art(9, 30.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    # Klartext-Hauptzeilen (Datenbasiert statt Standardkurve):
    assert "Ist-VL 35.0 °C bei Außen 10.0 °C" in vl["aktuell"]
    assert "Daten-Minimum 30.0 °C" in vl["aktuell"]
    assert "VL ~5.0 K zu hoch" in vl["aktuell"]
    assert "Raster" not in vl["aktuell"]  # Jargon wandert ins Detail
    assert "1 Raster absenken" in vl["vorgeschlagen"]
    assert f"Ziel-VL ~{30.0 - config.VL_RASTER_K:.1f} °C" in vl["vorgeschlagen"]
    # Jargon-Details (n + Lernstart + Band):
    assert "Datenbasiert (n=60 Heizstunden ab 07.11.:" in vl["detail"]
    assert "Außen-T-Band 9…12 °C" in vl["detail"]
    assert f"Ziel-VL-Soll bei Außen 10.0 °C = {30.0 - config.VL_RASTER_K:.1f} °C" \
        in vl["detail"]
    assert "Standard-Kurve" not in vl["aktuell"]  # datenbasiert, nicht Standard
    assert vl.get("detail") == vl["begruendung_de"]


def test_datenbasiert_e2e_anheben(tmp_path: Any, wx_ok: Any, skill_map: Any,
                                  monkeypatch: Any) -> None:
    """Ist-VL deutlich unter dem VL-Minimum (< −1 Raster) → +1 Raster
    (anheben, Komfort-Floor: Räume hielten am Minimum das Soll, das
    System ist jetzt darunter)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features"),
        ext=10.0,
        hz_vl=28.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    art = _min_art(9, 30.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "VL ~2.0 K zu niedrig" in vl["aktuell"]
    assert "1 Raster anheben" in vl["vorgeschlagen"]
    assert f"Ziel-VL ~{30.0 + config.VL_RASTER_K:.1f} °C" in vl["vorgeschlagen"]
    assert f"Ziel-VL-Soll bei Außen 10.0 °C = {30.0 + config.VL_RASTER_K:.1f} °C" \
        in vl["detail"]


def test_datenbasiert_e2e_beibehalten(tmp_path: Any, wx_ok: Any,
                                      skill_map: Any,
                                      monkeypatch: Any) -> None:
    """Ist-VL im Daten-Band (± 3 Raster um das Minimum) → beibehalten
    (VL im Daten-Band, Räume am Soll)."""
    store = _set_store_cols(
        make_mini_store(tmp_path / "features"),
        ext=10.0,
        hz_vl=32.0,
        oel_burner_on_sum3h=1.0,
        pump_hz_on_sum3h=0.0,
    )
    art = _min_art(9, 30.0)  # gap +2,0 K → im Band
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(
        TS, features_dir=store, wx=wx_ok, predictor=_p_eq(), skill_map=skill_map
    )
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "beibehalten" in vl["vorgeschlagen"]
    assert "Daten-Band" in vl["vorgeschlagen"]
    assert "VL im Daten-Band (± 3 Raster um das VL-Minimum)" in vl["detail"]


def test_datenbasiert_unit_tier_b() -> None:
    """Unit: datenbasierter Zweig via ``tier_b_advisories`` (State-Direkt,
    kein Cache-VL-Kalibrierungs-Bezug — Standardkurve-Heuristik entfällt)."""
    st = _state(ext=10.0, hz_vl=35.0)
    from analysis.model.l2.executability import load_discovery

    disc = load_discovery(config.INVENTAR_DEFAULT_PATH)
    adv = tier_b_advisories(st, None, [], disc, vl_min_art=_min_art(9, 30.0))
    vl = next(e for e in adv if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Datenbasiert" in vl["detail"]
    assert "1 Raster absenken" in vl["vorgeschlagen"]


# --- Kaskade: v0.5-Fallback (Stufe 3, Datenmangel) ---------------------------


def test_fallback_v005_vl_min_art_nicht_usable(tmp_path: Any, wx_ok: Any,
                                                skill_map: Any,
                                                monkeypatch: Any) -> None:
    """VL-Minimum-Cache vorhanden, aber usables Band für IST-Außen-T NICHT
    (n_am_soll < min_n) → Kaskade überspringt Stufe 2 → exakt v0.5
    (Standardkurve-Heuristik + „Kalibrierung nicht verfügbar“-Hinweis,
    Mini-Store: ext 9,0 → Kurve ≈ 45,7; Ist-VL 32,0 → dev ≤ −2 K →
    −1 Raster)."""
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    # Artefakt mit nur Band 12 (n=40 < 50) → für ext 9,0 (Band 9) kein
    # usables Band:
    art = _min_art(12, 40.0, n_am_soll=40, usable=False)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: art
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Kalibrierung nicht verfügbar" in vl["begruendung_de"]
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
    from analysis.model.m2b.features import vlst_est

    curve9 = float(vlst_est(np.array([9.0]))[0])
    assert f"Ziel-VL-Soll bei Außen 9.0 °C = {curve9 - config.VL_RASTER_K:.1f} °C" \
        in vl["begruendung_de"]


def test_fallback_v005_kein_cache(tmp_path: Any, wx_ok: Any, skill_map: Any,
                                   monkeypatch: Any) -> None:
    """Kein VL-Minimum-Cache (``None``) → exakt v0.5 (Regression:
    Datenmangel-Verhalten unverändert, Mini-Store ext 9,0 → −1 Raster)."""
    store = _set_store_cols(make_mini_store(tmp_path / "features"),
                            oel_burner_on_sum3h=1.0, pump_hz_on_sum3h=0.0)
    monkeypatch.setattr(
        "analysis.model.l2.recommend.load_vl_minimum", lambda *a, **k: None
    )
    from analysis.model.l2 import recommend as rec_mod

    rec = rec_mod.recommend(TS, features_dir=store, wx=wx_ok, predictor=_p_eq(),
                            skill_map=skill_map)
    vl = next(e for e in rec.empfehlungen if e["parameter"] == "Heizkurve/VL-Soll")
    assert "Kalibrierung nicht verfügbar" in vl["begruendung_de"]
    assert vl["vorgeschlagen"].startswith("Heizkurven-Absatz −1 Raster")
