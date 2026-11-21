"""L2 v0.6-A — Minderer-V→ΔT-Kennlinie (2-Faktor, last-normalisiert, Cache).

maintainer-Ziel (2026-11-17, HITL 18.11): die lineare V-Ziel-Schätzung in
:func:`l2.recommend.minderer_v_target_v` („erst linear annehmen, dann
messen, dann Kurve“) wird **ersetzt** durch eine datenbelegte V→ΔT-Kennlinie
je Strang; **Fallback linear bei < 2 Phasen-Punkten** pro Strang (altes
Verhalten exakt erhalten, wenn kein belastbarer Fit existiert).

**Physik (Kanon 2026-11-16/17):** V→ΔT ist NICHT linear-in-V: im Drossel-
Regime (Ventil offen, ~2–7 V) ≈ ΔT ≈ k/V (hyperbolisch, k ≈ 39–40 K·V bei
normaler Last); bei hohem V (~≥ 8–10 V) = **Sättigungs-Regime** (Ventil voll
offen, UVR610 regelt über die O8-Pumpe) → ΔT flach, der 1/V-Anteil bricht ab.
Deshalb **2-Faktor-Modell** ``ΔT = a·(1/V) + b·ext + c`` (hyperbolisch in V,
linear in Außen-T = Last-Normierung), lineares Least-Squares auf den
Features ``(1/V, ext, 1)`` → numpy (keine neue Dependency).

**Kennlinien-Punkte = NUR O8-AN + Zeitplan-NORMAL + Minderer-offen**
(Pump-ON-Flow, Phasen-Mittel/Steady-State): ``dT_offen`` = Mittel von
``VL − RL_Strang``, ``aussen_o`` = Mittel der Außen-T, ``n`` = Anzahl
(O8-AUS-Samples = reiner thermischer Drift/Backflow, **NIE** Kennlinien-
Punkt — Methodik-Korrektur 15.11, datenbewiesen). Datenbasis: 10-s-
Rohdaten ab Lernstart ``config.VL_MIN_LEARN_START`` (2026-11-07 16:14,
UVR-Programm-Upload; wiederverwendet — davor: keine statischen
Sollspannungen → nicht belastbar).

**Phasen-Erkennung:** konstante Sollspannung (a17/18/19, Toleranz ±0,05 V)
über aufeinanderfolgende Samples, Mindestdauer ≥ 1 h (schließt die
16-min-1-V-Phase automatisch aus — maintainer: „viel zu kurz“) + min. 30 Samples
+ max. Datenlücke 60 s innerhalb einer Phase (Segment-Bruch bei
Logger-Aussetzer). Zone-Mapping a17→kell / a18→eg / a19→dg = dokumentierte
Annahme (a18↔EG live bestätigt; s. ``config.RM_RAW_MINDERER_SOLL``).

**Regime-/Sättigungs-Prüfung (Zusatz 18.11):** das höchste-V-Phasen-Punkt
(aktuell 10 V: UVR duty 0,65, O8-Anteil 32 % — Ventil steht offen, O8 regelt)
liegt wahrscheinlich im Sättigungs-Regime und trifft ein reines 1/V-Modell
schlecht. Rule (deterministisch, dokumentiert): nach dem 2-Faktor-Fit
Residuum je Phasen-Punkt berechnen; liegt das Residuum des
``RM_CURVE_SAT_V``-Punkts (10 V) ≥ ``RM_CURVE_SAT_RESID_K`` (1,5 K) →
``out_of_throttle_regime = true`` und (bei ≥ ``RM_CURVE_SAT_REFIT_MIN_N``
verbleibenden Punkten) **Refit OHNE dem Punkt** → die Inverse
(:func:`v_from_dt_target`) nutzt die Drossel-Fit-Koeffizienten (10-V-Punkt
aus der Inverse **ausgeschlossen**, bleibt Kontext in ``points`` +
``satur_10v``). Kleines Residuum → Flag ``false``, ganzer Bereich [0, 10]
kontrollierbar.

**Einmaliges Cache-Artefakt** (Muster von ``calibrate-vl-min``):
``data/l2/minderer_curve.json`` (CLI ``python -m analysis.model.l2
calibrate-minderer-curve`` — einziger Build-Punkt). Pro ``recommend()`` nur
**Cache-Read** + Inverse (kein Historie-Scan). Kein CMI-Zugriff, kein
Live-Netz, deterministisch (kein RNG, DuckDB-Aggregationen).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np

from . import config

# Berlin-Zon (konsistent zu ``rm_diagnostics``/``vl_minimum``).
_BERLIN: ZoneInfo = ZoneInfo(config.TIMEZONE)


def _f(v: Any) -> float | None:
    """Rohwert → float (NaN/None → None)."""
    if v is None:
        return None
    x = float(v)
    return x if np.isfinite(x) else None


def _to_float(arr: Any) -> np.ndarray:
    """DuckDB-NumPy-Spalte (ggf. masked) → float64 mit NaN für NULL
    (Muster ``rm_diagnostics._to_float``)."""
    if hasattr(arr, "filled"):
        return np.asarray(arr.astype(np.float64).filled(np.nan))
    return np.asarray(arr, dtype=np.float64)


def minderer_curve_path(data_root: Path | None = None) -> Path:
    """Pfad des Minderer-Kurven-Artefakts (Muster ``vl_minimum_path``).

    - ``data_root=None`` → Repo-Default (``config.RM_CURVE_ESTIMATE_PATH``).
    - ``data_root`` (z. B. Gateway ``/data`` = Parent von ``/data/features``)
      → ``<data_root>/l2/minderer_curve.json`` (rw-``data``-Bind).
    """
    if data_root is None:
        return config.RM_CURVE_ESTIMATE_PATH
    return Path(data_root) / "l2" / "minderer_curve.json"


# --- Phasen-Erkennung (reine Logik, synthetisch testbar) ---------------------


def detect_phases(
    ts: np.ndarray,
    soll: np.ndarray,
    min_hours: float | None = None,
    min_samples: int | None = None,
    max_gap_s: float | None = None,
    soll_tol_v: float | None = None,
) -> list[dict[str, Any]]:
    """Phasen = konstante Sollspannung-Segmente (Kennlinien-Kandidaten).

    Deterministisch (sortierte Eingabe, kein RNG):

    1. Samples mit nicht-finitem ``soll`` NULLen (kein Phasen-Material).
    2. Quantisierung auf das Phasen-Raster ``round(soll / (2·tol))·2·tol``
       (Toleranz ± ``RM_CURVE_SOLL_TOL_V`` = 0,05 V auf dem 10-s-Raster —
       exakt die Abweichung, die UVR-ROUNDING produzieren kann).
    3. Maximal-Segmente mit unveränderter Rasterstufe; Bruch bei Wert-
       Wechsel **oder** Datenlücke > ``max_gap_s`` (Logger-Aussetzer
       zählt als Segment-Ende — konservativ: kurze Segmente scheiden
       über die Mindestdauer aus).
    4. Filter: Dauer ≥ ``min_hours`` h UND n ≥ ``min_samples``.

    Ausgabe je Phase: ``v`` (Rasterstufe), ``t_min``/``t_max`` (ISO,
    Berlin-naive wie die Rohdaten), ``n`` (Sample-Anzahl), ``i0``/``i1``
    (Index-Slice in die Eingabe-Arrays, ``i1`` exklusiv).
    """
    min_h = config.RM_CURVE_MIN_HOURS if min_hours is None else min_hours
    min_n = config.RM_CURVE_MIN_SAMPLES if min_samples is None else min_samples
    gap_s = config.RM_CURVE_MAX_GAP_S if max_gap_s is None else max_gap_s
    tol = config.RM_CURVE_SOLL_TOL_V if soll_tol_v is None else soll_tol_v
    if ts.size < 2:
        return []
    ts64 = np.asarray(ts, dtype="datetime64[s]")
    soll_f = np.asarray(soll, dtype=np.float64)
    good = np.isfinite(soll_f)
    if not good.any():
        return []
    # Raster (deterministisch; Toleranz symmetrisch → Quantisierung).
    raster = (np.round(soll_f / (2.0 * tol)) * 2.0 * tol).astype(np.float64)
    raster[~good] = np.nan
    phases: list[dict[str, Any]] = []
    i0 = int(np.argmax(good))
    n = ts.size
    cur_v = float(raster[i0])
    last = int(i0)
    seg_i0 = i0
    i = i0 + 1
    trailing = True
    while i < n:
        v_i = raster[i]
        gap = (ts64[i] - ts64[last]) / np.timedelta64(1, "s")
        if np.isnan(v_i) or v_i != cur_v or gap > gap_s:
            phases.append({"v": cur_v, "i0": seg_i0, "i1": last, "n": last - seg_i0 + 1})
            if np.isnan(v_i):
                # NULL-Lücke: Segment-Bruch; nächstes Good-Sample startet neu.
                nxt = int(np.argmax(good[i + 1 :])) if good[i + 1 :].any() else -1
                if nxt < 0:
                    trailing = False
                    break
                seg_i0 = i + 1 + nxt
                cur_v = float(raster[seg_i0])
                last = seg_i0
                i = seg_i0 + 1
                continue
            seg_i0 = i
            cur_v = float(v_i)
        last = i
        i += 1
    if trailing:
        phases.append({"v": cur_v, "i0": seg_i0, "i1": last, "n": last - seg_i0 + 1})
    out: list[dict[str, Any]] = []
    for p in phases:
        dur_s = (ts64[p["i1"]] - ts64[p["i0"]]) / np.timedelta64(1, "s")
        if dur_s >= min_h * 3600.0 and int(p["n"]) >= min_n:
            p["dur_h"] = float(dur_s / 3600.0)
            out.append(p)
    return out


def phase_metrics(
    ph: dict[str, Any],
    vl: np.ndarray,
    rl: np.ndarray,
    ext: np.ndarray,
    pump: np.ndarray,
    raum_soll: np.ndarray,
    minder_sig: np.ndarray,
) -> dict[str, Any] | None:
    """Kennlinien-Metrik einer Phase (O8-AN + NORMAL + Minderer-offen-Only).

    (Methodik s. Modul-Doku + Konsistenz zu ``tmp/v006a_eg_v_eval.py``):
    nur Samples mit ``pump == 1`` (O8-AN), ``raum_soll ≥ RM_CURVE_NORMAL_MIN_C``
    (Zeitplan NORMAL) **und** ``minder_sig > RM_CURVE_OFFEN_MIN_V`` (Minderer-
    An-Takt) zählen. ``dT_offen`` = Mittel(VL − RL), ``aussen_o`` = Mittel der
    Außen-T, ``n`` = Anzahl derer Samples. O8-AUS-Samples = reiner
    thermischer Drift → **nie** Kennlinien-Punkt.

    ``None`` wenn < 30 Kennlinien-Samples (keine belastbare Phase-Metrik →
    Phase wird nicht als Fit-Punkt verwendet).
    """
    i0, i1 = int(ph["i0"]), int(ph["i1"])
    v = vl[i0 : i1 + 1]
    r = rl[i0 : i1 + 1]
    e = ext[i0 : i1 + 1]
    p = pump[i0 : i1 + 1]
    rs = raum_soll[i0 : i1 + 1]
    ms = minder_sig[i0 : i1 + 1]
    mask = (
        np.isfinite(v)
        & np.isfinite(r)
        & np.isfinite(e)
        & np.isfinite(rs)
        & np.isfinite(ms)
        & (p == 1.0)
        & (rs >= config.RM_CURVE_NORMAL_MIN_C)
        & (ms > config.RM_CURVE_OFFEN_MIN_V)
    )
    n = int(mask.sum())
    if n < 30:
        return None
    dt_mean = float(np.mean(v[mask] - r[mask]))
    ext_mean = float(np.mean(e[mask]))
    return {
        "v": float(ph["v"]),
        "dt_offen": round(dt_mean, 4),
        "aussen_o": round(ext_mean, 4),
        "n": n,
    }


# --- Fit (2-Faktor, deterministisch) ------------------------------------------


def fit_2factor(points: list[tuple[float, float, float]]) -> dict[str, Any]:
    """Lineares Least-Squares ``ΔT = a·(1/V) + b·ext + c`` auf Phasen-Punkten.

    ``points`` = Liste von ``(V, ext, dT_offen)``. Features ``(1/V, ext, 1)``
    → numpy ``lstsq`` (kein RNG, deterministisch). Unsicherheit:
    Residual-σ (MSE-basiert, Freiheitsgrad n−3; bei exakt 3 Punkten = 0
    Freiheitsgrade → σ = 0,0, bei n < 3 = ``None`` — exakter 2-Punkt-Fit
    wäre „σ = n/a“; vgl. Cache-Flag ``usable`` + Advisory-Text).
    """
    pts = np.asarray(points, dtype=np.float64)
    x = pts[:, 0]
    e = pts[:, 1]
    y = pts[:, 2]
    n = int(x.size)
    if n < 3:
        if n == 2:
            # Exakter 2-Punkt-Fit: lineares 2×2-System in (a, b) mit c = 0
            # (Referenz-Außen-T im Modell; „exakter 2-Punkt-Fit, σ = n/a“ —
            # s. Task-DoD; die Konstante c ist bei 2 Punkten nicht bestimmt).
            a1, a2 = 1.0 / x[0], 1.0 / x[1]
            det = a1 * e[1] - a2 * e[0]
            if abs(det) < 1e-12:
                # Degeneriert (identische Feature-Kombination) → kein Fit
                # (Zahlen 0; der Zone-Entry bleibt usable=False-kalibriert,
                # die Inverse liefert bei c=0/b=0 das Guard-Verhalten).
                return {"a": 0.0, "b": 0.0, "c": 0.0, "resid_sigma": None}
            a = (y[0] * e[1] - y[1] * e[0]) / det
            b = (a1 * y[1] - a2 * y[0]) / det
            return {"a": float(a), "b": float(b), "c": 0.0, "resid_sigma": None}
        return {"a": 0.0, "b": 0.0, "c": 0.0, "resid_sigma": None}
    A = np.column_stack([1.0 / x, e, np.ones_like(x)])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    a, b, c = (float(v) for v in coef)
    resid = y - A @ coef
    dof = n - 3
    sigma = float(np.sqrt(np.sum(resid**2) / dof)) if dof > 0 else 0.0
    return {"a": a, "b": b, "c": c, "resid_sigma": sigma}


def v_from_dt_target(
    delta_t_c: float,
    current_v: float,
    target_c: float | None = None,
    ext_c: float | None = None,
    curve: dict[str, Any] | None = None,
) -> float:
    """V-Ziel (V, 0…10) aus ΔT-Ziel (v0.6-A: datenbasiert + linear-Fallback).

    **Datenbasiert (v0.6-A):** ``curve`` = Zone-Entry aus dem
    Minderer-Kurven-Cache (``usable=True``, mit ``a/b/c``), ``ext_c`` =
    IST-Außen-T → Inverse der 2-Faktor-Kennlinie
    ``V = a / (ΔT_ziel − b·ext − c)``. Guards:

    - Nenner ≤ ``RM_CURVE_INVERSE_DEN_MIN_K`` (0,5 K) → ``V = RM_V_MAX``
      (Lastterm allein übersteigt Ziel → nicht per Minderer erreichbar,
      öffnen/beibehalten — konsistent mit der bisherigen ``gap ≤ 0 →
      V_MAX``-Semantik).
    - Sonst ``clip(V, 0, 10)``; zusätzlich **Klemme ``min(V, v_now)``**
      (V-Ziel wird nur bei ΔT < Ziel − Toleranz aufgerufen; konservativ:
      nie über die aktuelle Vorgabe hinaus öffnen).

    **Fallback (v0.5, exakt linear-altes Verhalten):** ``curve=None``
    (kein Zone-Fit / < 2 Punkte / Cache fehlt) ODER ``ext_c=None`` (keine
    Außen-T) → ``V_aktuell · (1 − (Ziel − ΔT)/Ziel)``, clip [0, 10]
    (Regressionstest: wörtlich die Formel aus
    ``test_minderer_v_target_monotonic_and_capped``).

    ``current_v`` = **Ist-Vorgabe** = Sollspannung a17/18/19 (statischer
    Fixwert, UVR-extern setzbar; v0.5-engine-rework).
    """
    tgt = config.RM_DELTA_T_TARGET_C if target_c is None else target_c
    v_now = float(np.clip(current_v, 0.0, config.RM_V_MAX))
    # --- Fallback: linear (v0.5-Formel, wörtlich) ---
    use_curve = curve is not None and curve.get("usable") and ext_c is not None
    if not use_curve:
        gap = tgt - float(delta_t_c)
        if gap <= 0.0:
            return config.RM_V_MAX
        close_frac = gap / tgt if tgt > 0.0 else 1.0
        return float(np.clip(v_now * (1.0 - close_frac), 0.0, config.RM_V_MAX))
    # --- datenbasiert (v0.6-A): Inverse der 2-Faktor-Kennlinie ---
    # ``delta_t_c`` (Ist-ΔT) bestimmt nur den Advisory-Branch (drosseln/
    # öffnen/beibehalten) in :func:`_minderer_advisories`; die Inverse löst
    # nach dem V, das bei IST-Außen-T das **Ziel-ΔT** (``tgt``) erreicht:
    # ``ΔT(V) = a/V + b·ext + c = tgt`` → ``V = a / (tgt − b·ext − c)``.
    a = float(curve["a"])
    b = float(curve["b"])
    c = float(curve["c"])
    den = tgt - (b * float(ext_c) + c)
    if den <= config.RM_CURVE_INVERSE_DEN_MIN_K:
        return config.RM_V_MAX
    v = a / den
    v = float(np.clip(v, 0.0, config.RM_V_MAX))
    return float(min(v, v_now))


# --- Build (einmalig, DuckDB read-only) ---------------------------------------


def build_minderer_curve(
    data_dir: Path | None = None,
    now: datetime | str | None = None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Minderer-V→ΔT-Kennlinien-Bau (einmalig, caches-fähig) → JSON-Artefakt.

    Liest die 10-s-Rohdaten (``config.RM_RAW_STORES`` = winsol_pq +
    cmi_log_pq, Hive-Glob) **ab Lernstart** (``VL_MIN_LEARN_START``),
    erkennt je Zone (a17/18/19) die konstanten Sollspannungs-Phasen
    (Mindestdauer ≥ 1 h), wertet die O8-AN + NORMAL + Minderer-offen-
    Samples (``dT_offen``, ``aussen_o``, ``n``) aus und fitet je Zone das
    2-Faktor-Modell ``ΔT = a·(1/V) + b·ext + c`` (Least-Squares).

    **Regime-/Sättigungs-Prüfung:** Residuum je Punkt; 10-V-Punkt-Residuum
    ≥ ``RM_CURVE_SAT_RESID_K`` → ``out_of_throttle_regime = true`` + Refit
    OHNE dem Punkt (Inverse nutzt Drossel-Fit; 10-V bleibt Kontext).

    Deterministisch (kein RNG, DuckDB-Aggregationen); ``now`` =
    Artefakt-Zeitstempel (injizierbar).
    """
    if now is None:
        now = datetime.now(_BERLIN)
    elif isinstance(now, str):
        now = datetime.fromisoformat(now).replace(tzinfo=_BERLIN)
    now_b = now.astimezone(_BERLIN)
    base = Path(data_dir) if data_dir is not None else config._REPO_ROOT / "data"
    stores: list[str] = []
    for store in config.RM_RAW_STORES:
        if (base / store).is_dir():
            stores.append(str(store))
    if not stores:
        raise FileNotFoundError(
            f"Keine Rohdaten-Stores unter {base} (erwartet: {config.RM_RAW_STORES})"
        )
    zones = list(config.ZONES)
    # Spalten: ts + VL + Außen + Raum-SOLL.eff (NORMAL-Test, dev0 a12) +
    # Pumpe + RL/Minderer/Soll je Zone.
    cols: list[str] = [config.RM_RAW_VL, config.RM_RAW_EXT, "dev0_analog_12", config.RM_RAW_PUMP]
    cols += [config.RM_RAW_STRANG_RL[z] for z in zones]
    cols += [config.RM_RAW_MINDERER[z] for z in zones]
    cols += [config.RM_RAW_MINDERER_SOLL[z] for z in zones]
    unions: list[str] = []
    where = (
        f" WHERE cast(ts AS TIMESTAMP) >= TIMESTAMP '{config.VL_MIN_LEARN_START}'"
    )
    for store in stores:
        avail = _store_columns(base / store)
        if avail is None:
            continue
        select_parts = [
            c if c in avail else f"CAST(NULL AS DOUBLE) AS {c}" for c in cols
        ]
        unions.append(
            f"SELECT ts, {', '.join(select_parts)} FROM '{base / store / '*/*/*.parquet'}'{where}"
        )
    if not unions:
        raise FileNotFoundError(f"Kein lesbarer Rohdaten-Store unter {base}")
    sql = " UNION ALL ".join(unions)
    con = duckdb.connect()
    try:
        con.execute("SET enable_progress_bar = false")
        res = con.execute(f"SELECT ts, {', '.join(cols)} FROM ({sql}) ORDER BY ts").fetchnumpy()
    finally:
        con.close()
    ts = np.array([str(t) for t in res["ts"]], dtype="datetime64[s]")
    vl = _to_float(res[config.RM_RAW_VL])
    ext = _to_float(res[config.RM_RAW_EXT])
    raum_soll = _to_float(res["dev0_analog_12"])
    pump = _to_float(res[config.RM_RAW_PUMP])
    rl = {z: _to_float(res[config.RM_RAW_STRANG_RL[z]]) for z in zones}
    rm_sig = {z: _to_float(res[config.RM_RAW_MINDERER[z]]) for z in zones}
    soll_arr = {z: _to_float(res[config.RM_RAW_MINDERER_SOLL[z]]) for z in zones}

    artifact_zones: dict[str, Any] = {}
    for z in zones:
        artifact_zones[z] = _zone_entry(
            ts, soll_arr[z], vl, rl[z], ext, pump, raum_soll, rm_sig[z]
        )
    artifact = {
        "generated_at": now_b.strftime("%Y-%m-%d %H:%M:%S"),
        "learn_start": config.VL_MIN_LEARN_START,
        "method": (
            "Minderer-V→ΔT-Kennlinie je Strang (EG/Keller/DG), 2-Faktor-Modell "
            "ΔT = a·(1/V) + b·ext + c (Last-Normierung, lineares Least-Squares "
            "auf (1/V, ext, 1)); Kennlinien-Punkte = NUR O8-AN + NORMAL + "
            "Minderer-offen (Phasen-Mittel, min. 1 h konstante Sollspannung); "
            "Regime-/Sättigungs-Prüfung (10-V-Residuum ≥ 1,5 K → "
            "out_of_throttle_regime + Refit ohne 10-V); Inverse = a/(ΔT_ziel − "
            "b·ext − c), Nenner ≤ 0,5 K → V_MAX, Klemme min(V, v_now); "
            "einmaliges Cache-Artefakt, Rebuild = "
            "python -m analysis.model.l2 calibrate-minderer-curve"
        ),
        "zones": artifact_zones,
        "constants": {
            "RM_CURVE_SOLL_TOL_V": config.RM_CURVE_SOLL_TOL_V,
            "RM_CURVE_MIN_HOURS": config.RM_CURVE_MIN_HOURS,
            "RM_CURVE_MIN_SAMPLES": config.RM_CURVE_MIN_SAMPLES,
            "RM_CURVE_MAX_GAP_S": config.RM_CURVE_MAX_GAP_S,
            "RM_CURVE_NORMAL_MIN_C": config.RM_CURVE_NORMAL_MIN_C,
            "RM_CURVE_OFFEN_MIN_V": config.RM_CURVE_OFFEN_MIN_V,
            "RM_CURVE_MIN_POINTS": config.RM_CURVE_MIN_POINTS,
            "RM_CURVE_INVERSE_DEN_MIN_K": config.RM_CURVE_INVERSE_DEN_MIN_K,
            "RM_CURVE_EXT_RANGE_WARN_K": config.RM_CURVE_EXT_RANGE_WARN_K,
            "RM_CURVE_SAT_V": config.RM_CURVE_SAT_V,
            "RM_CURVE_SAT_RESID_K": config.RM_CURVE_SAT_RESID_K,
            "RM_CURVE_SAT_REFIT_MIN_N": config.RM_CURVE_SAT_REFIT_MIN_N,
            "RM_DELTA_T_TARGET_C": config.RM_DELTA_T_TARGET_C,
            "RM_V_MAX": config.RM_V_MAX,
        },
    }
    out = Path(out_path) if out_path is not None else minderer_curve_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact


def _zone_entry(
    ts: np.ndarray,
    soll: np.ndarray,
    vl: np.ndarray,
    rl: np.ndarray,
    ext: np.ndarray,
    pump: np.ndarray,
    raum_soll: np.ndarray,
    rm_sig: np.ndarray,
) -> dict[str, Any]:
    """Je-Strang-Entry: Phasen → Metriken → Fit → Sättigungs-Prüfung."""
    phases = detect_phases(ts, soll)
    points: list[dict[str, Any]] = []
    for ph in phases:
        met = phase_metrics(ph, vl, rl, ext, pump, raum_soll, rm_sig)
        if met is None:
            continue
        met["ts_min"] = str(ts[ph["i0"]])
        met["ts_max"] = str(ts[ph["i1"]])
        points.append(met)
    points.sort(key=lambda p: p["v"])
    # Defensive: Punkte mit v ≤ 0 (degeneriertes Raster) sind unphysikalisch
    # (Minderer zu → kein Flow) → aus dem Fit entfernen.
    points = [p for p in points if p["v"] > 0.0]
    n_points = len(points)
    usable = n_points >= config.RM_CURVE_MIN_POINTS
    base_pts = [(p["v"], p["aussen_o"], p["dt_offen"]) for p in points]
    sat_v = config.RM_CURVE_SAT_V
    sat_point = next((p for p in points if abs(p["v"] - sat_v) < 1e-9), None)
    sat_resid = None
    out_of_throttle = False
    if n_points >= 3 and sat_point is not None:
        base_fit = fit_2factor(base_pts)
        pred_sat = (
            base_fit["a"] / sat_point["v"]
            + base_fit["b"] * sat_point["aussen_o"]
            + base_fit["c"]
        )
        sat_resid = round(sat_point["dt_offen"] - pred_sat, 4)
        out_of_throttle = abs(sat_resid) >= config.RM_CURVE_SAT_RESID_K
    if out_of_throttle and n_points - 1 >= config.RM_CURVE_SAT_REFIT_MIN_N:
        fit = fit_2factor([p for p in base_pts if abs(p[0] - sat_v) >= 1e-9])
        fit_source = "drossel_regime_ohne_10v"
    elif n_points >= 2:
        fit = fit_2factor(base_pts)
        fit_source = "all_points"
    else:
        # < 2 Punkte: kein belastbarer Fit (Fallback linear) → Placeholder
        # (nur strukturell, wird NICHT in den Entry geschrieben, s. ``usable``).
        fit = {"a": 0.0, "b": 0.0, "c": 0.0, "resid_sigma": None}
        fit_source = "none"
    resid_by_point: list[dict[str, Any]] = []
    # ext-Band = das Band der **Fit-Punkte** (bei Sättigungs-Refit: OHNE dem
    # 10-V-Punkt — dessen Außen-T gehört nicht zum kontrollierbaren
    # Drossel-Regime-Band).
    fit_pts = [pt for pt in base_pts if not (out_of_throttle and abs(pt[0] - sat_v) < 1e-9)]
    # Residuen je Punkt nur gegen einen echten Fit (n_points ≥ 2); ohne Fit
    # wäre das Residuum gegen das (0,0,0)-Placeholder-Modell irreführend.
    for p in points:
        if p["v"] <= 0.0 or n_points < 2:
            continue  # degenerierter Punkt / kein Fit (Defensive)
        pred = fit["a"] / p["v"] + fit["b"] * p["aussen_o"] + fit["c"]
        resid_by_point.append(
            {"v": p["v"], "resid_k": round(p["dt_offen"] - pred, 4)}
        )
    ext_vals = [pt[1] for pt in fit_pts if pt[0] > 0.0]
    entry: dict[str, Any] = {
        "n_points": n_points,
        "usable": usable,
        "fit_source": fit_source,
        "out_of_throttle_regime": out_of_throttle,
        "satur_10v_resid_k": sat_resid,
        "residuals": resid_by_point,
        "satur_10v": None,
        "ext_min": round(min(ext_vals), 2) if ext_vals else None,
        "ext_max": round(max(ext_vals), 2) if ext_vals else None,
        "points": points,
        "learn_start": config.VL_MIN_LEARN_START,
    }
    if out_of_throttle and sat_point is not None:
        entry["satur_10v"] = sat_point
    if usable:
        entry["a"] = round(fit["a"], 6)
        entry["b"] = round(fit["b"], 6)
        entry["c"] = round(fit["c"], 6)
        entry["resid_sigma"] = (
            round(fit["resid_sigma"], 6) if fit["resid_sigma"] is not None else None
        )
    return entry


def _store_columns(root: Path) -> set[str] | None:
    """Verfügbare Spalten eines Roh-Stores (Muster ``rm_diagnostics``)."""
    files = sorted(Path(root).glob("*/*/*.parquet"))
    if not files:
        return None
    try:
        con = duckdb.connect()
        try:
            res = con.execute(f"SELECT * FROM '{files[0]}' LIMIT 0")
            names = {d[0] for d in res.description or ()}
        finally:
            con.close()
        return names
    except Exception:  # noqa: BLE001 - Schema-Fehler → Store überspringen
        return None


def load_minderer_curve(path: Path | None = None) -> dict[str, Any] | None:
    """Minderer-Kurven-Artefakt laden; ``None`` wenn fehlt/ungültig (→
    der datenbasierte Zweig entfällt, linear-Fallback bleibt exakt)."""
    p = Path(path) if path is not None else config.RM_CURVE_ESTIMATE_PATH
    if not p.exists():
        return None
    try:
        art = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(art, dict) or "zones" not in art:
        return None
    return art


def zone_entry(
    art: dict[str, Any] | None, zone: str
) -> dict[str, Any] | None:
    """Usables Zone-Entry für die Inverse (Pro-Call, Cache-Read).

    Nur Zonen mit ``usable=True`` (n_points ≥ 2) **und** vorhandenem
    ``a/b/c`` sind verwendbar. ``None`` (kein Artefakt / Zone nicht
    kalibriert) → linear-Fallback in :func:`v_from_dt_target`.
    """
    if not art:
        return None
    ent = (art.get("zones") or {}).get(zone)
    if ent and ent.get("usable") and ent.get("a") is not None:
        return ent
    return None


def ext_oob_flag(
    art: dict[str, Any] | None, zone: str, ext_c: float | None
) -> bool:
    """Out-of-Range-Außen-T-Flag (Advisory-Text-Flag, **kein Stopp**).

    ``True`` wenn die IST-Außen-T außerhalb des Fit-Kalibrierungsbands
    (``ext_min``/``ext_max`` der Phasen-Punkte) liegt ±
    ``RM_CURVE_EXT_RANGE_WARN_K`` (Default 0,0 = exakt am Band-Rand).
    Kein Artefakt / Zone nicht usable / ``ext_c=None`` → ``False``
    (kein Hinweis, Fallback-Verhalten unverändert).
    """
    if ext_c is None:
        return False
    ent = zone_entry(art, zone)
    if ent is None:
        return False
    lo = ent.get("ext_min")
    hi = ent.get("ext_max")
    if lo is None or hi is None:
        return False
    warn = config.RM_CURVE_EXT_RANGE_WARN_K
    return ext_c < lo - warn or ext_c > hi + warn


__all__ = [
    "build_minderer_curve",
    "detect_phases",
    "fit_2factor",
    "load_minderer_curve",
    "minderer_curve_path",
    "phase_metrics",
    "v_from_dt_target",
    "zone_entry",
    "ext_oob_flag",
]
