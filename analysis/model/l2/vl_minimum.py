"""L2 v0.6-B — L2-Heizkurve v2: „VL-Minimum s.t. Räume am Soll“ (Cache-Bau + -Lesung).

maintainer-Zielfunktion (2026-11-12, maßgeblich): „VL so niedrig wie möglich,
Räume halten trotzdem die Temperatur“ — **Pumpen-Laufzeit maximiert ist die
FOLGE, nicht das Ziel.** Die erste Fähigkeit des Heizkurven-Blocks:

- **Datenbasiertes VL-Minimum** pro Außen-T-Band (3 K, konsistent mit
  :mod:`l2.vl_calibration`): „Wie tief darf die Kurve pro Außen-T-Band,
  bevor ein Raum < Soll − Toleranz fällt?“ — gelernt aus den Daten (nur
  **ab Lernstart** ``VL_MIN_LEARN_START`` = 2026-11-07 16:14 belastbar;
  davor: keine statischen Sollspannungen, RAS-Ist/Soll ab P=181 = 11-07).

Datenmodell (M1-Features-Store, stündliche Buckets):

- **Heizstunde** = ``pump_hz_on ≥ VL_KALIB_HEAT_EPS`` (0,5 — gleiche Maske
  wie die v0.5-Kalibrierung) UND ``ts ≥ VL_MIN_LEARN_START``.
- Je Zone: Raum-Abweichung = ``ras_{z}_ist − (RAS_BASE_C − offset_{z})``
  (v0.3-Ableitung, identisch zum RAS-Offset-Block in
  ``recommend._zone_advice``). „Raum am Soll“ = Abweichung ≥
  −``VL_MIN_AM_SOLL_TOL_K`` (1,5 K, dokumentierte Toleranz). Ein Raum zählt
  in einer Stunde nur mit, wenn sein RAS-Ist geloggt ist (≠ NULL).
- **vl_min_soll** pro Band = das ``VL_MIN_QUANTILE``.-Perzentil (95.) der
  ``hz_vl``-Werte in den Heizstunden, in denen **alle aktiven Räume** am
  Soll waren. 95.-Perzentil (begründet, dokumentiert): die Kurve darf so
  tief, dass in ~95 % der relevanten Heizstunden der Vorlauf so niedrig
  oder höher war — Komfort-Risiko (Raum < Soll − Toleranz) ≤ ~5 %.
- **n_am_soll < VL_MIN_MIN_N** (50, dokumentiert) → Band „nicht
  kalibriert“ (``usable=False``) → :mod:`l2.recommend` fällt auf die
  v0.5-Standardkurve-Logik zurück (Datenmangel-Verhalten exakt erhalten).

**Einmaliges Cache-Artefakt** (Muster von ``calibrate-vl``):
``data/l2/vl_minimum.json`` (CLI ``python -m analysis.model.l2
calibrate-vl-min`` — einziger Build-Punkt). Pro ``recommend()`` nur
**Cache-Read** + Vergleich gegen IST-VL/Außen-T (kein Historie-Scan).

**Recover** (die zweite v0.6-B-Fähigkeit, live ohne Cache) lebt in
:mod:`l2.recommend` (Kaskade-Stufe 1); :func:`recover_n_raster` liefert
dazu die Raster-Klemmung (``N = min(2, ceil((|dev| − 1,5 K)/VL_RASTER_K))``,
Trigger-Grenze ≥ 1). Die UVR610 regelt die Raum-T selbst aktiv (reaktiv:
ABSENK/NORMAL-Auswertung + RAS) → L2 ergänzt nur antizipativ und zählt das
**nicht doppelt** (Klartext-Hinweis im Advisory).

Kein CMI-Zugriff, kein Live-Netz, deterministisch (kein RNG, DuckDB-
Aggregationen).
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np

from . import config
from .vl_calibration import band_for_ext

# Berlin-Zon (konsistent zu ``rm_diagnostics``/``weather``/``vl_calibration``).
_BERLIN: ZoneInfo = ZoneInfo(config.TIMEZONE)
# Lernstart kurzform für die Klartext-Präsentation (DD.MM., aus der
# Konstante abgeleitet — bleibt synchron mit ``VL_MIN_LEARN_START``).
LEARN_START_SHORT: str = datetime.strptime(
    config.VL_MIN_LEARN_START, "%Y-%m-%d %H:%M:%S"
).strftime("%d.%m.")

# Store-Spalten (Feature-Store m1.3; Spalten-Beleg per ``pq.read_schema``
# 2026-11-12 verifiziert).
_COL_EXT = "ext"
_COL_VL = "hz_vl"
_COL_PUMP = "pump_hz_on"
_ZONE_COLS: tuple[str, ...] = tuple(
    f for z in config.ZONES for f in (f"ras_{z}_ist", f"offset_{z}")
)


def _f(v: Any) -> float | None:
    """Rohwert → float (NaN/None → None)."""
    if v is None:
        return None
    x = float(v)
    return x if np.isfinite(x) else None


def vl_minimum_path(data_root: Path | None = None) -> Path:
    """Pfad des VL-Minimum-Artefakts (Muster ``vl_calibration_path``).

    - ``data_root=None`` → Repo-Default (``config.VL_MIN_ESTIMATE_PATH``).
    - ``data_root`` (z. B. Gateway ``/data`` = Parent von ``/data/features``)
      → ``<data_root>/l2/vl_minimum.json`` (rw-``data``-Bind).
    """
    if data_root is None:
        return config.VL_MIN_ESTIMATE_PATH
    return Path(data_root) / "l2" / "vl_minimum.json"


def _am_soll_devs(devs: dict[str, float]) -> bool:
    """„Alle aktiven Räume am Soll“: alle geloggten Zonen mit
    Abweichung ≥ −``VL_MIN_AM_SOLL_TOL_K``. Leeres Dict (kein RAS-Ist
    geloggt) → ``False`` (keine Am-Soll-Belegung möglich)."""
    if not devs:
        return False
    return all(d >= -config.VL_MIN_AM_SOLL_TOL_K for d in devs.values())


def calibrate_vl_min(
    features_dir: Path | None = None,
    now: datetime | str | None = None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """VL-Minimum-Kalibrierung (einmalig, caches-fähig) → JSON-Artefakt.

    Liest den Feature-Store (``data/features``, READ-ONLY), maskiert auf
    **Heizstunden** (``pump_hz_on ≥ VL_KALIB_HEAT_EPS``) **ab Lernstart**
    (``ts ≥ VL_MIN_LEARN_START``) und gruppiert nach **Außen-T-Bändern**
    (``VL_KALIB_BAND_K`` = 3 K, ``band_for_ext`` — konsistent mit
    :mod:`l2.vl_calibration`): je Band ``n_heizstunden`` +
    ``n_am_soll`` (Heizstunden, in denen alle aktiven Räume am Soll
    waren) + ``vl_min_soll`` (``VL_MIN_QUANTILE``.-Perzentil der ``hz_vl``
    über die Am-Soll-Stunden) + ``usable`` (``n_am_soll ≥
    VL_MIN_MIN_N``) + ``konfidenz`` (``min(1, n_am_soll/(2·min_n))``, 0,0
    wenn nicht usable). Deterministisch (keine Zeit-/RNG-Abhängigkeit
    außer dem injizierbaren ``now`` = Artefakt-Zeitstempel).
    """
    if now is None:
        now = datetime.now(_BERLIN)
    elif isinstance(now, str):
        now = datetime.fromisoformat(now).replace(tzinfo=_BERLIN)
    now_b = now.astimezone(_BERLIN)
    feats = str(Path(features_dir) if features_dir else config.FEATURES_DIR)
    cols = ", ".join((_COL_EXT, _COL_VL, *_ZONE_COLS))
    con = duckdb.connect()
    try:
        res = con.execute(
            f"SELECT ts, {cols} FROM '{feats}/*/*/*.parquet' "
            f"WHERE {_COL_PUMP} >= {config.VL_KALIB_HEAT_EPS} "
            f"AND ts >= TIMESTAMP '{config.VL_MIN_LEARN_START}'"
        ).fetchall()
    finally:
        con.close()

    band_of = int(config.VL_KALIB_BAND_K)
    bands: dict[int, dict[str, Any]] = {}
    n_heat = 0
    for row in res:
        ts_val, ext, vl, *zone_vals = row
        e = _f(ext)
        v = _f(vl)
        if e is None or v is None:
            continue
        n_heat += 1
        devs: dict[str, float] = {}
        for z, ist_raw, off_raw in zip(
            config.ZONES, zone_vals[0::2], zone_vals[1::2], strict=True
        ):
            ist = _f(ist_raw)
            if ist is not None:
                off = _f(off_raw)
                devs[z] = ist - (config.RAS_BASE_C - (off if off is not None else 0.0))
        b = band_for_ext(e)
        ent = bands.setdefault(
            b, {"ext_sum": 0.0, "n_heat": 0, "n_am_soll": 0, "vls": []}
        )
        ent["ext_sum"] += e
        ent["n_heat"] += 1
        if _am_soll_devs(devs):
            ent["n_am_soll"] += 1
            ent["vls"].append(v)

    out_bands: dict[str, dict[str, Any]] = {}
    for b in sorted(bands):
        ent = bands[b]
        ext_mean = ent["ext_sum"] / ent["n_heat"]
        n_am = int(ent["n_am_soll"])
        usable = n_am >= config.VL_MIN_MIN_N
        out_bands[str(b)] = {
            "band_lo": b,
            "band_hi": b + band_of,
            "ext_mean": round(ext_mean, 2),
            "n_heizstunden": int(ent["n_heat"]),
            "n_am_soll": n_am,
            "vl_min_soll": (
                round(float(np.percentile(ent["vls"], config.VL_MIN_QUANTILE)), 2)
                if ent["vls"]
                else None
            ),
            "usable": usable,
            "konfidenz": (
                round(min(1.0, n_am / (2 * config.VL_MIN_MIN_N)), 2) if usable else 0.0
            ),
        }
    artifact = {
        "generated_at": now_b.strftime("%Y-%m-%d %H:%M:%S"),
        "learn_start": config.VL_MIN_LEARN_START,
        "method": (
            "VL-Minimum je Außen-T-Band (3 K): 95.-Perzentil der hz_vl in den "
            "Heizstunden (pump_hz_on ≥ 0,5, ab Lernstart 2026-11-07 16:14), in "
            "denen alle aktiven Räume am Soll waren (Abweichung ≥ −1,5 K); "
            "einmaliges Cache-Artefakt, Rebuild = "
            "python -m analysis.model.l2 calibrate-vl-min"
        ),
        "quantile": int(config.VL_MIN_QUANTILE),
        "am_soll_tol_k": config.VL_MIN_AM_SOLL_TOL_K,
        "min_n_per_band": int(config.VL_MIN_MIN_N),
        "band_k": band_of,
        "n_heizstunden": n_heat,
        "bands": out_bands,
        "constants": {
            "VL_MIN_LEARN_START": config.VL_MIN_LEARN_START,
            "VL_MIN_AM_SOLL_TOL_K": config.VL_MIN_AM_SOLL_TOL_K,
            "VL_MIN_MIN_N": config.VL_MIN_MIN_N,
            "VL_MIN_QUANTILE": int(config.VL_MIN_QUANTILE),
            "VL_RASTER_K": config.VL_RASTER_K,
            "VL_KALIB_HEAT_EPS": config.VL_KALIB_HEAT_EPS,
        },
    }
    out = Path(out_path) if out_path is not None else vl_minimum_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact


def load_vl_minimum(path: Path | None = None) -> dict[str, Any] | None:
    """VL-Minimum-Artefakt laden; ``None`` wenn fehlt/ungültig (→ der
    datenbasierte Kaskaden-Zweig entfällt, v0.5-Verhalten bleibt exakt)."""
    p = Path(path) if path is not None else config.VL_MIN_ESTIMATE_PATH
    if not p.exists():
        return None
    try:
        art = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(art, dict) or "bands" not in art:
        return None
    return art


def band_at(art: dict[str, Any] | None, ext_c: float) -> dict[str, Any] | None:
    """Usables VL-Minimum-Band für die IST-Außen-T (Pro-Call, Cache-Read).

    Band = ``band_for_ext(ext_c)`` (3 K, konsistent mit
    :mod:`l2.vl_calibration`); nur Bands mit ``usable=True`` (n_am_soll ≥
    ``VL_MIN_MIN_N``) **und** vorhandenem ``vl_min_soll`` sind
    verwendbar. ``None`` (kein Artefakt / Band nicht kalibriert / kein
    Am-Soll-Material) → datenbasierter Kaskaden-Zweig entfällt
    (→ v0.5-Standardkurve-Fallback in :mod:`l2.recommend`).
    """
    if not art:
        return None
    ent = (art.get("bands") or {}).get(str(band_for_ext(ext_c)))
    if ent and ent.get("usable") and ent.get("vl_min_soll") is not None:
        return ent
    return None


def recover_n_raster(dev_k: float) -> int:
    """Recover-Raster (Kaskade-Stufe 1): ``N = min(2,
    ceil((|dev| − 1,5 K)/VL_RASTER_K))``, Klemme ≥ 1.

    ``dev_k`` = Raum-Abweichung Ist − Soll (K, negativ = unter Soll);
    Trigger ist ``dev ≤ −VL_MIN_RECOVER_GAP_K`` (−1,5 K). An der
    Trigger-Grenze (exakt −1,5 K) ist die Formel 0 → mindestens 1 Raster
    (minimale Absatzerhöhung, dokumentiert). ``VL_MIN_RECOVER_MAX_RASTER``
    = 2 = Kappe (keine drastischen Sprünge aus einer Live-Stunde).
    """
    gap = abs(dev_k) - config.VL_MIN_RECOVER_GAP_K
    n = math.ceil(gap / config.VL_RASTER_K)
    return int(max(1, min(config.VL_MIN_RECOVER_MAX_RASTER, n)))


__all__ = [
    "LEARN_START_SHORT",
    "band_at",
    "calibrate_vl_min",
    "load_vl_minimum",
    "recover_n_raster",
    "vl_minimum_path",
]
