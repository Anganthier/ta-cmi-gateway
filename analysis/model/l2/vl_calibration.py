"""L2 v0.5 Ticket C — VL-Soll-Kalibrierung (einmaliges Cache-Artefakt).

Ersetzt im überarbeiteten Tier-B-Block das methodisch falsche
„0,1-K-Diff-Prinzip“ (VL-vs-``hz_rl``-jetzt; Q-124: VL↔RL-Lag ist pro Etage
unterschiedlich UND durchflussabhängig → direkter 1-h-Vergleich passt
selten). Neue Methodik (maintainer-Zielbild „VL so niedrig wie möglich“):

- **Vergleich:** tatsächlicher VL-Ø (``hz_vl``, **global** — VL-Soll-Kalibrierung
  betrifft den Vorlauf, kein pro-Etage-Lag hier nötig) × **Heizstunden-Only**
  (``pump_hz_on ≥ VL_KALIB_HEAT_EPS``, Maske) × **Außen-T-Bänder**
  (``VL_KALIB_BAND_K`` = 3 K) **gegen die UVR-Standard-Heizkurve**
  (m2b ``vlst_est`` / ``HK_CURVE``).
- **Einmaliges Cache-Artefakt** (Muster von Ticket D):
  ``data/l2/vl_kalibrierung.json`` (CLI ``python -m analysis.model.l2
  calibrate-vl``). Pro ``recommend()``-Aufruf nur **Cache-Read** + Vergleich
  gegen IST-VL/Außen-T (wie die Lags) — **kein** Historie-Scan.
  Trennung dokumentiert: Rebuild-Befehl = ``calibrate-vl`` (Heizsaison-Fenster
  ``VL_KALIB_WINDOW_DAYS``).
- **Fehlt/ungültig → None** → ``recommend()`` fällt auf das v0.4-Verhalten
  (Kurve-Absatz-Heuristik) + Hinweis „Kalibrierung nicht verfügbar“ zurück.

Kein CMI-Zugriff, kein Live-Netz, deterministisch (kein RNG, DuckDB-
Aggregationen).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np

from analysis.model.m2b.features import vlst_est

from . import config

# Berlin-Zon (konsistent zu ``rm_diagnostics``/``weather``).
_BERLIN: ZoneInfo = ZoneInfo(config.TIMEZONE)
# Store-Spalten (Feature-Store m1.3, Spalten-Präsenz im Store verifiziert).
_COL_EXT = "ext"
_COL_VL = "hz_vl"
_COL_PUMP = "pump_hz_on"
_COL_OEL = "oel_burner_on"


def _f(v: Any) -> float | None:
    """Rohwert → float (NaN/None → None)."""
    if v is None:
        return None
    x = float(v)
    return x if np.isfinite(x) else None


def vl_calibration_path(data_root: Path | None = None) -> Path:
    """Pfad des Kalibrierungs-Artefakts (Muster ``lag_estimate_path``).

    - ``data_root=None`` → Repo-Default (``config.VL_KALIB_ESTIMATE_PATH``).
    - ``data_root`` (z. B. Gateway ``/data`` = Parent von ``/data/features``)
      → ``<data_root>/l2/vl_kalibrierung.json`` (rw-``data``-Bind).
    """
    if data_root is None:
        return config.VL_KALIB_ESTIMATE_PATH
    return Path(data_root) / "l2" / "vl_kalibrierung.json"


def calibrate_vl(
    features_dir: Path | None = None,
    window_days: int | None = None,
    now: datetime | str | None = None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Band-Kalibrierung (einmalig, caches-fähig) → JSON-Artefakt.

    Liest den Feature-Store (``data/features``, READ-ONLY) über das
    Heizsaison-Fenster (Default ``config.VL_KALIB_WINDOW_DAYS`` = 365 d),
    maskiert auf **Heizstunden** (``pump_hz_on ≥ VL_KALIB_HEAT_EPS``) und
    gruppiert nach **Außen-T-Bändern** (``VL_KALIB_BAND_K`` = 3 K): je Band
    VL-Ø + n. Vergleichswert = UVR-Standard-Heizkurve ``vlst_est(band_mid)``.

    Bänder mit ``n < VL_KALIB_MIN_N`` → ``usable=False`` (zu wenig
    Heizstunden → Heuristik-Fallback). Deterministisch (keine Zeit-/RNG-
    Abhängigkeit außer dem injizierbaren Fenster-Ende ``now``).
    """
    if now is None:
        now = datetime.now(_BERLIN)
    elif isinstance(now, str):
        now = datetime.fromisoformat(now).replace(tzinfo=_BERLIN)
    now_b = now.astimezone(_BERLIN)
    win = int(window_days if window_days is not None else config.VL_KALIB_WINDOW_DAYS)
    since = (now_b - timedelta(days=win)).strftime("%Y-%m-%d %H:%M:%S")
    feats = str(Path(features_dir) if features_dir else config.FEATURES_DIR)
    con = duckdb.connect()
    try:
        res = con.execute(
            f"SELECT ts, {_COL_EXT}, {_COL_VL}, {_COL_OEL} "
            f"FROM '{feats}/*/*/*.parquet' WHERE {_COL_PUMP} >= {config.VL_KALIB_HEAT_EPS} "
            f"AND ts >= TIMESTAMP '{since}'"
        ).fetchall()
    finally:
        con.close()

    band_of = int(config.VL_KALIB_BAND_K)
    bands: dict[int, dict[str, Any]] = {}
    n_heat = 0
    n_oel = 0
    for _ts, ext, vl, oel in res:
        e = _f(ext)
        v = _f(vl)
        o = _f(oel)
        if e is None or v is None:
            continue
        n_heat += 1
        if o is not None and o >= 0.5:
            n_oel += 1
        b = int(np.floor(e / band_of)) * band_of
        ent = bands.setdefault(b, {"ext_sum": 0.0, "vl_sum": 0.0, "n": 0})
        ent["ext_sum"] += e
        ent["vl_sum"] += v
        ent["n"] += 1

    out_bands: dict[str, dict[str, Any]] = {}
    for b in sorted(bands):
        ent = bands[b]
        ext_mean = ent["ext_sum"] / ent["n"]
        vl_mean = ent["vl_sum"] / ent["n"]
        curve = float(vlst_est(np.array([ext_mean]))[0])
        out_bands[str(b)] = {
            "band_lo": b,
            "band_hi": b + band_of,
            "ext_mean": round(ext_mean, 2),
            "vl_mean": round(vl_mean, 2),
            "curve_vl_soll": round(curve, 2),
            "dev_k": round(vl_mean - curve, 2),
            "n": int(ent["n"]),
            "usable": ent["n"] >= config.VL_KALIB_MIN_N,
        }
    artifact = {
        "generated_at": now_b.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": win,
        "window_since": since,
        "method": (
            "VL-Ø je Außen-T-Band (3 K) × Heizstunden-Only (pump_hz_on ≥ "
            f"{config.VL_KALIB_HEAT_EPS}) gegen UVR-Standard-Heizkurve "
            "(m2b vlst_est/HK_CURVE); einmaliges Cache-Artefakt, Rebuild = "
            "python -m analysis.model.l2 calibrate-vl"
        ),
        "n_heizstunden": n_heat,
        "n_oel_brenner_stunden": n_oel,
        "min_n_per_band": config.VL_KALIB_MIN_N,
        "band_k": band_of,
        "bands": out_bands,
        "constants": {
            "VL_KALIB_WINDOW_DAYS": config.VL_KALIB_WINDOW_DAYS,
            "VL_KALIB_BAND_K": config.VL_KALIB_BAND_K,
            "VL_KALIB_MIN_N": config.VL_KALIB_MIN_N,
            "VL_KALIB_HEAT_EPS": config.VL_KALIB_HEAT_EPS,
            "VL_RASTER_K": config.VL_RASTER_K,
        },
    }
    out = Path(out_path) if out_path is not None else vl_calibration_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact


def load_vl_calibration(path: Path | None = None) -> dict[str, Any] | None:
    """Kalibrierungs-Artefakt laden; ``None`` wenn fehlt/ungültig (→ v0.4-
    Heuristik-Fallback in ``recommend()``)."""
    p = Path(path) if path is not None else config.VL_KALIB_ESTIMATE_PATH
    if not p.exists():
        return None
    try:
        art = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(art, dict) or "bands" not in art:
        return None
    return art


def band_for_ext(ext_c: float) -> int:
    """Außen-T-Band (unten, ``floor(ext / VL_KALIB_BAND_K) * band_k``).

    Negative Außen-T: floor (z. B. −1 °C → Band −3; Band [−3, 0)).
    """
    return int(np.floor(ext_c / config.VL_KALIB_BAND_K)) * int(config.VL_KALIB_BAND_K)


def usable_bands(art: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Verwendbare Bänder (``usable`` = n ≥ min_n, sortiert nach Band-Unterkante).

    ``None`` (kein Artefakt) → leere Liste (→ v0.4-Heuristik).
    """
    if not art:
        return []
    bands = [b for b in (art.get("bands") or {}).values() if b.get("usable")]
    return sorted(bands, key=lambda b: b.get("band_lo", 0))


def curve_deviation_at(
    art: dict[str, Any] | None,
    ext_c: float,
    vl_now: float,
) -> tuple[float, dict[str, Any] | None]:
    """Kalibrierung-Deviation bei IST-Außen-T (Pro-Call, Cache-Read).

    Bänder mit ausreichend Heizstunden: ``dev = VL-Ø_Band −
    vlst_est(Ø_Außen-T_Band)`` (geschätzte Kurven-Abweichung im aktuellen
    Außen-T-Band). Falls **kein** usables Band existiert oder das aktuelle
    Band nicht kalibriert ist → Fallback auf ``vl_now − vlst_est(ext_c)``
    (Ist-VL vs Standard-Kurve; ``band=None``). Returns ``(dev, band_or_None)``.
    """
    band = band_for_ext(ext_c)
    if art:
        for b in usable_bands(art):
            if b.get("band_lo") == band:
                return float(b["dev_k"]), b
    return vl_now - float(vlst_est(np.array([ext_c]))[0]), None


def raster_sign(dev_k: float) -> int:
    """Raster-Direktion der Kurven-Abweichung (konservativ,
    ``VL_RASTER_K`` = 1 K ≈ 0,5–1 K VL-Soll pro Raster).

    ``dev`` = VL-Ø(Heizstunden, Band) − Standard-Heizkurve:

    - ``dev > +VL_RASTER_K`` → System lief im Heizbetrieb **wärmer** als die
      Standard-Kurve → die Kurve kann um 1 Raster **abgesenkt** werden
      (−1; Zielbild „VL so niedrig wie möglich“ — Komfort war im Heizbetrieb
      bei dem höheren VL gehalten, Setpoint senkbar).
    - ``dev < −VL_RASTER_K`` → System lief **kälter** als die Standard-Kurve
      (+1; Abweichungs-Direktion). Die Zielbild-Politik (VL-Minimierung,
      Komfort via RAS-Offsets) wertet das in :func:`recommend` als
      **beibehalten** (kein Anheben empfohlen; kein data-belegtes weiteres
      Absenken — Komfort-Floor).
    - sonst 0 (im Raster → beibehalten).

    (Rein richtungsgebend; die Zielbild-Entscheidung (Absenken nur bei
    ``dev > 0``, nie Anheben) liegt im Advisory-Block von ``recommend``.)
    """
    if dev_k > config.VL_RASTER_K:
        return -1
    if dev_k < -config.VL_RASTER_K:
        return 1
    return 0


__all__ = [
    "band_for_ext",
    "calibrate_vl",
    "curve_deviation_at",
    "load_vl_calibration",
    "raster_sign",
    "usable_bands",
    "vl_calibration_path",
]
