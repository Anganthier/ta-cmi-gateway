"""L2 v0.5 Ticket D — ΔT-Diagnose + pro-Etage-Lag + Konditions-Flag.

Fundament für das Zielbild „VL so niedrig wie möglich“ (maintainer-Beauftragung
2026-11-07, Plan.md §"L2 v0.5" + CIR-Log Q-124). Rein **diagnostisch/advisory**:
erzeugt NEUE Datenfelder (ΔT je Etage, Lag je Etage, Konditions-Flag), die die
Tickets A/B/C (Minderer/Pumpe/Heizkurve) + die UI später konsumieren.

Kernidee (maintainer, maßgeblich): VL → Strang-RL hat einen **pro Etage
unterschiedlichen UND durchflussabhängigen** Zeitversatz (Lag) → direkter
1-h-Vergleich passt selten. Deshalb:

1. **Lag-Schätzung (einmalig, caches-fähig):** Kreuzkorrelation
   (VL[t] vs Strang-RL[t+L]) auf den **10-s-Rohdaten** (``winsol_pq`` +
   ``cmi_log_pq``). Output: Lag je Etage + Konfidenz + n; Default-Lag-Fallback,
   wenn n klein (Keller-Kreis läuft kaum). Ergebnis wird als JSON-Artefakt
   persistiert (``data/l2/rm_lag_estimate.json``) — **kein** pro-``recommend()``-
   Neu-Scan der ganzen Historie (aufwendig).
2. **Pro-Call-ΔT (leicht):** VL/Rohdaten-jetzt (letztes 2-h-Fenster) mit dem
   zuletzt geschätzten Lag. ΔT = VL(lag-korrigiert) − Strang-RL(jetzt).
   Fallback (Rohdaten nicht lesbar): stündliches Feature-Store-2-h-Ø.
3. **Konditions-Flag „≥ 1 Kreis braucht Wärme“:** Pumpen-3-h-Ø ≥ EPS ODER
   Minderer-offen-2-h-Ø ≥ 0,5 ODER RAS-perzipiert < Intent − 0,5 K (≥ 1 Zone).
   Inaktiv → Block stumm (maintainer-Bedingung: „VL niedrig“ ist sonst nicht
   definiert).

Datenbasis (verifiziert 2026-11-07, s. :mod:`l2.config` ``RM_RAW_*``):
Rohkanal-Mapping dev0/dev2 (UVR1611/UVR610); Feature-Store m1.3 für Zustand +
Kondition. Minderer a10–a12 (``RM_RAW_MINDERER``) sind das **interne
UVR-Signal „Minderer aktuell auf/zu“** (HzRkl V2): sie pendeln zwischen
0 V (zu) und der Sollspannung aus a17/18/19 (auf) und werden von der
internen UVR610-Regelung gesetzt — kein Positions-Sensor (F8). Der
Minderer-offen-Ø dient als Durchfluss-Proxy. Die **statische
Sollvorgabe/Fixwert** (die einstellbare Größe, von außen/UVR-Programm
setzbar) ist a17–a19 (``RM_RAW_MINDERER_SOLL``): Median über das Roh-
Fenster = ``minderer_soll_v`` je Zone (Default 10,0 V bei NULL).

Kein CMI-Zugriff, kein Live-Netz, deterministisch (kein RNG).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np

from . import config

# Bezeichnung der Zonen im ΔT/Lag-Kontext (1:1 zu ``config.ZONES``).
_ZONES: tuple[str, ...] = config.ZONES
# Durchfluss-Klassen-Schwellen (Minderer-offen-Ø, 0–1).
_FLOW_OPEN = 0.5
_FLOW_CLOSED = 0.1
# Berlin-Zon (für das Schätzungs-Fenster, ohne Weather-Import).
_BERLIN: ZoneInfo = ZoneInfo(config.TIMEZONE)


# --- Konditions-Flag -------------------------------------------------------


def _f(v: Any) -> float | None:
    """Rohwert → float (NaN/None → None)."""
    if v is None:
        return None
    x = float(v)
    return x if np.isfinite(x) else None


def _h(v: Any) -> float:
    """Rohwert → float (NaN/None → 0,0), für An-Quotienten/Summen."""
    if v is None:
        return 0.0
    x = float(v)
    return x if np.isfinite(x) else 0.0


def _mean(vals: list[float]) -> float:
    """Mittel über endliche Werte (leer → 0,0)."""
    vals = [v for v in vals if np.isfinite(v)]
    return float(np.mean(vals)) if vals else 0.0


@dataclass
class RmCondition:
    """Konditions-Flag „≥ 1 Kreis braucht Wärme“ (maintainer-Bedingung).

    ``active`` = True, wenn **eine** der drei OR-Bedingungen erfüllt ist:

    - Pumpen-3-h-Ø ≥ ``RM_PUMP_3H_EPS`` (Heizkreispumpe O8 lief in den
      letzten 3 h substantiell an) ODER
    - Minderer-offen-2-h-Ø ≥ ``RM_ACTIVE_QUOTA`` in ≥ 1 Etage (Kreis heizt)
      ODER
    - RAS-perzipiert < Intent − ``RM_RAS_INTENT_GAP_K`` in ≥ 1 Zone (Raum
      spürbar kälter als Intent → RAS heizt aktiv).

    ``active=False`` → „VL so niedrig wie möglich“ ist **nicht definiert**
    (kein Kreis braucht Wärme) → der ΔT/Lag-Block ist stumm (kein Advisory).
    """

    active: bool
    pump_3h_avg: float
    rm_open_2h: dict[str, float]
    ras_gap: dict[str, float]
    active_zones: list[str]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "pump_3h_avg": round(self.pump_3h_avg, 4),
            "rm_open_2h": {z: round(v, 4) for z, v in self.rm_open_2h.items()},
            "ras_gap": {z: round(v, 3) for z, v in self.ras_gap.items()},
            "active_zones": list(self.active_zones),
            "reasons": list(self.reasons),
        }


def rm_condition(rows: dict[str, Any]) -> RmCondition:
    """Konditions-Flag aus einer Store-Zeile (m1.3).

    Liest (aus **einer** Zeile, wie ``minderer_state``/``heating_active``):
    ``pump_hz_on_sum3h`` (0–3), ``hzrkl_{z}_on`` + ``_on_lag1`` (2-h-Fenster)
    und ``ras_{z}_perc`` + ``offset_{z}`` (Intent = ``RAS_BASE_C − offset``).
    NULL-Fail-Open: fehlende Spalten → Beitrag 0 / keine Aktivierung (keine
    Falsch-Aktivierung; der Minderer- + RAS-Beitrag tragen die Kondition).
    """
    pump3_sum = _f(rows.get("pump_hz_on_sum3h"))
    pump_3h_avg = (pump3_sum / 3.0) if (pump3_sum is not None and pump3_sum >= 0.0) else 0.0
    pump_hit = pump_3h_avg >= config.RM_PUMP_3H_EPS

    rm_open_2h: dict[str, float] = {}
    for z in _ZONES:
        a = _f(rows.get(f"hzrkl_{z}_on"))
        b = _f(rows.get(f"hzrkl_{z}_on_lag1"))
        parts = [v for v in (a, b) if v is not None]
        rm_open_2h[z] = _mean(parts) if parts else 0.0
    rm_hits = {z for z in _ZONES if rm_open_2h[z] >= config.RM_ACTIVE_QUOTA}

    ras_gap: dict[str, float] = {}
    for z in _ZONES:
        perz = _f(rows.get(f"ras_{z}_perc"))
        off = _f(rows.get(f"offset_{z}"))
        if perz is None:
            ras_gap[z] = 0.0
            continue
        intent = config.RAS_BASE_C - (off if off is not None else 0.0)
        ras_gap[z] = perz - intent  # < 0 = kälter als Intent
    ras_hits = {z for z in _ZONES if ras_gap[z] < -config.RM_RAS_INTENT_GAP_K}

    active = bool(pump_hit or rm_hits or ras_hits)
    reasons: list[str] = []
    if pump_hit:
        reasons.append(f"Pumpen-3-h-Ø {pump_3h_avg:.2f} ≥ {config.RM_PUMP_3H_EPS}")
    for z in sorted(rm_hits):
        reasons.append(f"Minderer {z} offen-2-h-Ø {rm_open_2h[z]:.2f} ≥ {config.RM_ACTIVE_QUOTA}")
    for z in sorted(ras_hits):
        reasons.append(
            f"RAS {z} perzipiert {abs(ras_gap[z]):.2f} K unter Intent "
            f"(> {config.RM_RAS_INTENT_GAP_K} K)"
        )
    # Zonen-Reihenfolge = ``config.ZONES`` (deterministisch, UI-Konsistenz).
    active_set = rm_hits | ras_hits
    if pump_hit:
        active_set = set(_ZONES)
    active_zones = [z for z in _ZONES if z in active_set]
    return RmCondition(
        active=active,
        pump_3h_avg=pump_3h_avg,
        rm_open_2h=rm_open_2h,
        ras_gap=ras_gap,
        active_zones=active_zones,
        reasons=reasons,
    )


# --- Lag-Schätzung (einmalig, caches-fähig) ---------------------------------


@dataclass
class RawWindow:
    """10-s-Rohdaten-Fenster (VL + Strang-RL je Etage + Minderer + Pumpe).

    Alle Arrays sind auf gemeinsame endliche VL-Indizes ausgerichtet
    (``ts`` in ``datetime64[s]``). ``n`` = Länge.
    """

    ts: np.ndarray
    vl: np.ndarray
    rl: dict[str, np.ndarray]  # {"kell","eg","dg","central"}
    rm: dict[str, np.ndarray]  # Minderer „auf/zu“-Signal 0–10 V je Etage (a10–12)
    pump: np.ndarray  # 0/1
    soll: dict[str, np.ndarray] = field(default_factory=dict)  # Sollspannung
    # je Etage (a17–19, statische Vorgabe, 0–10 V; leer = Legacy-/Synthese-
    # Fenster ohne Sollkanal → ``minderer_soll_v`` = Default 10,0 V).
    n: int = 0

    def soll_v(self, zone: str, default: float | None = None) -> float:
        """Minderer-Sollspannung je Etage (v0.5-engine-rework): **30-min-
        Median** über das Ende des Roh-Fensters (a17–19). Kanal fehlt/NULL
        oder kein endlicher Wert → ``default`` (Standard
        ``config.RM_MINDERER_SOLL_DEFAULT_V`` = 10,0 V)."""
        arr = self.soll.get(zone)
        if arr is not None and arr.size:
            cutoff_s = 1800.0  # 30 min
            n_tail = int(np.ceil(cutoff_s / self.dt_s)) if self.dt_s > 0 else 0
            seg = arr[-n_tail:] if n_tail > 0 else arr
            fin = seg[np.isfinite(seg)]
            if fin.size:
                return float(np.median(fin))
        d = config.RM_MINDERER_SOLL_DEFAULT_V if default is None else default
        return float(d)

    @property
    def dt_s(self) -> float:
        """Mediane Abtastperiode in Sekunden (≈ 10)."""
        if self.n < 2:
            return 10.0
        # ``datetime64[s]``: ``np.diff`` → timedelta64[s] (EINHEIT Sekunden).
        d = (np.diff(self.ts) / np.timedelta64(1, "s")).astype("float64")
        d = d[d > 0]
        return float(np.median(d)) if d.size else 10.0


def _raw_roots(data_dir: Path | None = None) -> list[Path]:
    """Rohdaten-Stores (Hive) je ``config.RM_RAW_STORES`` (nur vorhandene)."""
    base = Path(data_dir) if data_dir is not None else config._REPO_ROOT / "data"
    roots: list[Path] = []
    for store in config.RM_RAW_STORES:
        p = base / store
        if p.is_dir():
            roots.append(p)
    return roots


def _raw_columns() -> list[str]:
    """Rohkanal-Spalten je ``RawWindow``: VL + zentrale/Strang-RL +
    Minderer-Signal (a10–12) + **Sollspannung (a17–19)** + Pumpe."""
    cols = [config.RM_RAW_VL, config.RM_RAW_CENTRAL_RL]
    cols += list(config.RM_RAW_STRANG_RL.values())
    cols += list(config.RM_RAW_MINDERER.values())
    cols += list(config.RM_RAW_MINDERER_SOLL.values())
    cols += [config.RM_RAW_PUMP]
    return list(dict.fromkeys(cols))


def _to_float(arr: Any) -> np.ndarray:
    """DuckDB-NumPy-Spalte (ggf. masked) → float64 mit NaN für NULL."""
    if hasattr(arr, "filled"):
        return np.asarray(arr.astype(np.float64).filled(np.nan))
    return np.asarray(arr, dtype=np.float64)


def _store_columns(root: Path) -> set[str] | None:
    """Verfügbare Spalten eines Roh-Stores (Schema der ersten Datei).

    ``None`` wenn nicht lesbar (→ Store wird übersprungen). Die Stores sind
    **nicht** schema-identisch: ``cmi_log_pq`` (P181) trägt die
    Minderer-Sollspannungen ``dev2_analog_17–19``, ``winsol_pq`` (ältere
    Aufnahme) NICHT → pro Store werden fehlende Spalten als NULL gecastet
    (Union-Schema bleibt gleich, ``minderer_soll_v`` → Default 10,0 V).
    """
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


def _read_raw_since(
    data_dir: Path | None,
    since: str,
    until: str | None = None,
) -> RawWindow | None:
    """10-s-Rohdaten ab ``since`` (bis ``until``) aus beiden Stores laden.

    ``None``, wenn kein Store lesbar / keine Zeilen. Deterministisch (sortiert
    nach ``ts``). NULL/ungültige VL-Indizes werden gefiltert (finite-VL).
    ``ts`` ist VARCHAR (ISO) → lexicographische WHERE-Pushdown (ISO-Reihenfolge
    = Zeitreihenfolge) statt Vollmonats-Ladung (schnell für 2-h-Fenster).
    Fehlende Spalten je Store (z. B. ``dev2_analog_17–19`` in ``winsol_pq``)
    werden als NULL gecastet — das Union-Schema bleibt ``_raw_columns()``.
    """
    roots = _raw_roots(data_dir)
    if not roots:
        return None
    cols = _raw_columns()
    unions: list[str] = []
    where = f" WHERE ts >= '{since}'" + (f" AND ts <= '{until}'" if until else "")
    for r in roots:
        avail = _store_columns(r)
        if avail is None:
            continue
        # Pro Store: jede Spalte von ``cols`` in exakter ``cols``-Reihenfolge —
        # vorhanden → Spalte, fehlend (z. B. a17–19 in ``winsol_pq``) →
        # ``CAST(NULL AS DOUBLE)``. UNION ALL aligniert POSITIONELL, daher muss
        # die SELECT-Reihenfolge in jedem Ast ``cols`` exakt spiegeln
        # (pump ist letzte Spalte; fehlende Spalten am ANFANG des Blocks
        # anhängen würde die Positionen verschieben).
        select_parts = [
            c if c in avail else f"CAST(NULL AS DOUBLE) AS {c}"
            for c in cols
        ]
        unions.append(f"SELECT ts, {', '.join(select_parts)} FROM '{str(r / '*/*/*.parquet')}'{where}")
    if not unions:
        return None
    sql = " UNION ALL ".join(unions)
    con = duckdb.connect()
    try:
        con.execute("SET enable_progress_bar = false")
        res = con.execute(f"SELECT ts, {', '.join(cols)} FROM ({sql}) ORDER BY ts").fetchnumpy()
    except Exception:  # noqa: BLE001 - Rohdaten-Fehler → None (Fallback)
        con.close()
        return None
    con.close()
    if res is None or "ts" not in res:
        return None
    ts = np.asarray(res["ts"], dtype=object)
    ts = np.array([str(t) for t in ts]).astype("datetime64[s]")
    mask = ts >= np.datetime64(since)
    if until is not None:
        mask &= ts <= np.datetime64(until)
    ts = ts[mask]
    if ts.size == 0:
        return None
    arrays = {c: _to_float(res[c])[mask] for c in cols}
    vl = arrays[config.RM_RAW_VL]
    fin = np.isfinite(vl)
    ts = ts[fin]
    vl = vl[fin]
    rl: dict[str, np.ndarray] = {"central": arrays[config.RM_RAW_CENTRAL_RL][fin]}
    for z in _ZONES:
        rl[z] = arrays[config.RM_RAW_STRANG_RL[z]][fin]
    rm: dict[str, np.ndarray] = {z: arrays[config.RM_RAW_MINDERER[z]][fin] for z in _ZONES}
    soll: dict[str, np.ndarray] = {}
    for z in _ZONES:
        key = config.RM_RAW_MINDERER_SOLL.get(z)
        if key is not None:
            soll[z] = arrays[key][fin]
    pump = arrays[config.RM_RAW_PUMP][fin]
    return RawWindow(ts=ts, vl=vl, rl=rl, rm=rm, soll=soll, pump=pump, n=int(ts.size))


def _to_grid(
    ts: np.ndarray,
    vals: dict[str, np.ndarray],
    dt_s: int,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """10-s-Regular-Grid (forward-fill) über das VL-Fenster.

    ``ts`` (datetime64[s]) wird auf ein äquidistantes ``dt_s``-Raster
    (Start = ``ts[0]``) resampled; fehlende Samples werden per
    forward-fill aufgefüllt (Rohdaten sind ~lückenlos 10 s). Returns
    (grid_ts, {name: float-Array (NaN, wenn noch kein Wert)}).
    """
    t0 = ts[0]
    n = int((ts[-1] - t0).astype("int64") // dt_s) + 1
    grid = t0 + np.arange(n) * np.timedelta64(dt_s, "s")
    out: dict[str, np.ndarray] = {}
    for name, v in vals.items():
        arr = np.full(n, np.nan)
        # Vorwärtsfüllen: letzter Rohwert <= grid_t (hold-last; Punkte vor dem
        # ersten Sample bleiben NaN).
        pos = np.searchsorted(ts, grid, side="right") - 1
        valid = (pos >= 0) & (pos < v.size)
        arr[valid] = v[pos[valid]]
        out[name] = arr
    return grid, out


def _cross_corr_lag(
    ts: np.ndarray,
    vl_grid: np.ndarray,
    rl_grid: np.ndarray,
    active: np.ndarray,
    dt_s: float,
    lag_max_s: int,
    step_s: int,
    min_std: float,
    min_overlap: int,
) -> tuple[int, float, int] | None:
    """Bestes L ≥ 0 (Sekunden), das corr(VL[t], RL[t+L]) maximiert.

    Arbeitet auf dem **zeitlich ausgerichteten** 10-s-Grid: ``RL[t+L]`` ist der
    RL-Wert L Sekunden *später* (VL führt, RL folgt). ``active`` maskiert die
    Pumpen-an-Samples (Durchfluss-Klasse „offen“). Returns
    ``(lag_s, corr, n_overlap)`` oder ``None`` (kein gültiger L).
    Deterministisch: strenges ``>``-Update → erster (kleinstes) L bei Ties.
    """
    n = int(vl_grid.size)
    best_l = -1
    best_r = -1.0
    best_n = 0
    for L in range(0, lag_max_s + 1, step_s):
        k = int(round(L / dt_s))
        idx = np.arange(n - k) if k > 0 else np.arange(n)
        if idx.size < min_overlap:
            continue
        xv = vl_grid[idx]
        yv = rl_grid[idx + k]
        m = np.isfinite(xv) & np.isfinite(yv) & active[idx]
        if m.sum() < min_overlap:
            continue
        xv, yv = xv[m], yv[m]
        sx, sy = xv.std(), yv.std()
        if sx < min_std or sy < min_std:
            continue
        r = float(np.corrcoef(xv, yv)[0, 1])
        if np.isfinite(r) and r > best_r:
            best_r = r
            best_l = L
            best_n = int(m.sum())
    if best_l < 0:
        return None
    return best_l, best_r, best_n


def _lag_confidence(corr: float, n: int, sharp: float) -> float:
    """Heuristische Konfidenz (0–1): Korrelationsstärke + Stichprobe + Schärfe.

    - 0,5 · |corr| (starker Peak = zuverlässig)
    - 0,25 · min(n/40 000, 1) (große Stichprobe = stabil)
    - 0,25 · max(sharp, 0) (spitzer Peak gegenüber Nachbarn = eindeutiger Lag)
    """
    conf = 0.5 * abs(corr) + 0.25 * min(n / 40000.0, 1.0) + 0.25 * max(sharp, 0.0)
    return float(np.clip(conf, 0.0, 1.0))


def estimate_lags(
    data_dir: Path | None = None,
    window_days: int = 30,
    now: datetime | str | None = None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Pro-Etage-Lag-Schätzung (10-s-Rohdaten, Kreuzkorrelation) → JSON-Artefakt.

    **Einmalig, caches-fähig:** liest die letzten ``window_days`` 10-s-Rohdaten
    aus ``config.RM_RAW_STORES``, schätzt je Etage den VL→Strang-RL-Lag
    (Kreuzkorrelation VL[t] vs RL[t+L] über das aktive Pumpen- + Minderer-
    offen-Fenster) und persistiert ``{out_path}`` (Default
    ``config.RM_LAG_ESTIMATE_PATH``). **Kein** pro-``recommend()``-Aufruf.

    n < ``RM_LAG_MIN_SAMPLES`` (z. B. Keller: Kreis läuft kaum) →
    ``RM_DEFAULT_LAG_S`` + ``source="default"`` + ``confidence=0``.
    Deterministisch (kein RNG, sortiert, strenges ``>``-Update).
    """
    out = Path(out_path) if out_path is not None else lag_estimate_path(data_dir)
    if now is None:
        now = datetime.now(_BERLIN)
    elif isinstance(now, str):
        now = datetime.fromisoformat(now).replace(tzinfo=_BERLIN)
    now_b = now.astimezone(_BERLIN)
    # ISO mit SPACE (STORE-Schema „YYYY-MM-DD HH:MM:SS“): lexikographisch
    # vergleichbar in der WHERE-Pushdown. (``str(np.datetime64)`` würde „T“
    # liefern → falsche Reihenfolge.)
    since = (now_b - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    until = now_b.strftime("%Y-%m-%d %H:%M:%S")
    win = _read_raw_since(data_dir, since, until)
    n_total = int(win.n) if win is not None else 0

    zones: dict[str, dict[str, Any]] = {}
    if win is not None:
        dt_s = win.dt_s
        step_s = max(1, int(round(dt_s)))
        grid_ts, grid = _to_grid(win.ts, {"vl": win.vl, "pump": win.pump}, step_s)
        for z in list(_ZONES) + ["central"]:
            _, grid_rl = _to_grid(win.ts, {"rl": win.rl[z]}, step_s)
            rl_g = grid_rl["rl"]
            if z in win.rm:
                _, grid_rm = _to_grid(win.ts, {"rm": win.rm[z]}, step_s)
                active = (grid["pump"] > 0.5) & (grid_rm["rm"] > 5.0)
            else:
                active = (grid["pump"] > 0.5)
            active &= np.isfinite(grid_rl["rl"])
            n_active = int(active.sum())
            if n_active < config.RM_LAG_MIN_SAMPLES:
                zones[z] = {
                    "lag_s": config.RM_DEFAULT_LAG_S,
                    "confidence": 0.0,
                    "source": "default",
                    "n": n_active,
                    "corr": None,
                }
                continue
            res = _cross_corr_lag(
                grid_ts,
                grid["vl"],
                rl_g,
                active,
                dt_s,
                config.RM_LAG_SEARCH_MAX_S,
                config.RM_LAG_STEP_S,
                config.RM_LAG_MIN_STD_C,
                config.RM_LAG_MIN_OVERLAP,
            )
            if res is None:
                zones[z] = {
                    "lag_s": config.RM_DEFAULT_LAG_S,
                    "confidence": 0.0,
                    "source": "default",
                    "n": n_active,
                    "corr": None,
                }
                continue
            lag_s, corr, n_ov = res
            # Schärfe: Peak minus Ø der ±2-Step-Nachbarn (Lag-Kurve).
            neighbours: list[float] = []
            for off in (-2, -1, 1, 2):
                L2 = lag_s + off * config.RM_LAG_STEP_S
                if L2 < 0:
                    continue
                k2 = int(round(L2 / dt_s))
                idx2 = np.arange(grid_ts.size - k2)
                xv = grid["vl"][idx2]
                yv = rl_g[idx2 + k2]
                m2 = np.isfinite(xv) & np.isfinite(yv) & active[idx2]
                if m2.sum() < config.RM_LAG_MIN_OVERLAP:
                    continue
                xv, yv = xv[m2], yv[m2]
                if xv.std() < config.RM_LAG_MIN_STD_C or yv.std() < config.RM_LAG_MIN_STD_C:
                    continue
                r2 = float(np.corrcoef(xv, yv)[0, 1])
                if np.isfinite(r2):
                    neighbours.append(r2)
            sharp = float(corr - np.mean(neighbours)) if neighbours else 0.0
            zones[z] = {
                "lag_s": int(lag_s),
                "confidence": round(_lag_confidence(corr, n_ov, sharp), 3),
                "source": "estimate",
                "n": n_ov,
                "corr": round(corr, 3),
            }
    else:
        for z in list(_ZONES) + ["central"]:
            zones[z] = {
                "lag_s": config.RM_DEFAULT_LAG_S,
                "confidence": 0.0,
                "source": "default",
                "n": 0,
                "corr": None,
            }

    artifact = {
        "generated_at": now_b.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": int(window_days),
        "method": (
            "cross-correlation VL(dev0 a6) vs Strang-RL(dev2 a13/14/15) on "
            "10-s raw (winsol_pq+cmi_log_pq), active = pump on & Minderer open; "
            "Default-Lag-Fallback wenn n klein (Keller)"
        ),
        "n_samples_total": n_total,
        "zones": zones,
        "constants": {
            "RM_DELTA_T_TARGET_C": config.RM_DELTA_T_TARGET_C,
            "RM_DEFAULT_LAG_S": config.RM_DEFAULT_LAG_S,
            "RM_LAG_MIN_SAMPLES": config.RM_LAG_MIN_SAMPLES,
            "RM_LAG_SEARCH_MAX_S": config.RM_LAG_SEARCH_MAX_S,
            "RM_LAG_STEP_S": config.RM_LAG_STEP_S,
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact


def load_lag_estimate(path: Path | None = None) -> dict[str, Any] | None:
    """Lag-Artefakt (Cache) laden; ``None`` wenn fehlt/ungültig (→ Defaults)."""
    p = Path(path) if path is not None else config.RM_LAG_ESTIMATE_PATH
    if not p.exists():
        return None
    try:
        art = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(art, dict) or "zones" not in art:
        return None
    return art


def lag_estimate_path(data_root: Path | None = None) -> Path:
    """Pfad des Lag-Artefakts.

    - ``data_root=None`` → Repo-Default (``config.RM_LAG_ESTIMATE_PATH``).
    - ``data_root`` (z. B. Gateway-Container ``/data`` = Parent von
      ``/data/features``) → ``<data_root>/l2/rm_lag_estimate.json`` (das
      Artefakt lebt im rw-``data``-Bind, neben den Rohdaten-Stores).
    """
    if data_root is None:
        return config.RM_LAG_ESTIMATE_PATH
    return Path(data_root) / "l2" / "rm_lag_estimate.json"


def lag_for_zone(art: dict[str, Any] | None, zone: str) -> tuple[int, float, str]:
    """(lag_s, confidence, source) je Etage; Fallback = Default-Lag."""
    if art and zone in (art.get("zones") or {}):
        z = art["zones"][zone]
        lag = int(z.get("lag_s", config.RM_DEFAULT_LAG_S))
        conf = float(z.get("confidence", 0.0))
        src = str(z.get("source", "estimate"))
        return lag, conf, src
    return config.RM_DEFAULT_LAG_S, 0.0, "default"


# --- Pro-Call-ΔT (leicht) ---------------------------------------------------


def _dt_raw_lag_corrected(win: RawWindow, zone: str, lag_s: int) -> float | None:
    """ΔT = VL(lag-korrigiert) − Strang-RL(jetzt), Ø über das Roh-Fenster.

    VL führt, RL folgt: RL wird um den Lag (Samples) nach vorn verschoben,
    damit VL(t) mit dem RL(t+lag) verglichen wird. ``None`` wenn unzureichend
    gültige Paare.
    """
    if zone not in win.rl:
        return None
    dt_s = win.dt_s
    k = int(round(lag_s / dt_s)) if dt_s > 0 else 0
    n = win.n
    idx = np.arange(min(n - k, n)) if k >= 0 else np.arange(-k, n)
    if idx.size < 10:
        return None
    vl_v = win.vl[idx]
    rl_v = win.rl[zone][idx + k] if k >= 0 else win.rl[zone][idx]
    m = np.isfinite(vl_v) & np.isfinite(rl_v)
    if m.sum() < 10:
        return None
    return float(np.mean(vl_v[m] - rl_v[m]))


def _dt_hourly(rows: dict[str, Any], zone: str) -> float | None:
    """ΔT = VL − Strang-RL (stündlich, 2-h-Ø aus Feature-Store m1.3).

    2-h-Ø über zwei Stunden: (t) ``hz_vl`` − ``hzrkl_{z}_pos`` und (t−1)
    ``hz_vl_lag1`` − ``hzrkl_{z}_pos_last``. Sub-stündlicher Lag (< 1 h) ist
    im Stunden-Ø inhärent gemittelt → keine explizite Lag-Versetzung möglich.
    ``None`` wenn beide Stunden fehlen.
    """
    pairs: list[float] = []
    vl_now = _f(rows.get("hz_vl"))
    rl_now = _f(rows.get(f"hzrkl_{zone}_pos"))
    if vl_now is not None and rl_now is not None:
        pairs.append(vl_now - rl_now)
    vl_lag = _f(rows.get("hz_vl_lag1"))
    rl_lag = _f(rows.get(f"hzrkl_{zone}_pos_last"))
    if vl_lag is not None and rl_lag is not None:
        pairs.append(vl_lag - rl_lag)
    if not pairs:
        return None
    return float(np.mean(pairs))


@dataclass
class RmDiagnostics:
    """ΔT-Diagnose + Lag + Konditions-Flag (Pro-Call)."""

    condition: RmCondition
    per_zone: dict[str, dict[str, Any]]
    target_c: float
    active: bool
    raw_window_s: int = 0
    lag_source: dict[str, str] = field(default_factory=dict)

    @property
    def active_zones(self) -> list[str]:
        return self.condition.active_zones

    def to_dict(self) -> dict[str, Any]:
        # Inaktiv → Block stumm (maintainer-Bedingung): per_zone wird nicht
        # ausgegeben (nur Flag + Grund), damit A/B/C/UI stumm bleiben.
        per_zone = self.per_zone if self.active else {}
        return {
            "active": self.active,
            "condition": self.condition.to_dict(),
            "delta_t_target_c": self.target_c,
            "raw_window_s": self.raw_window_s,
            "lag_source": dict(self.lag_source),
            "per_zone": per_zone,
        }


def compute_diagnostics(
    rows: dict[str, Any],
    lag_art: dict[str, Any] | None = None,
    raw: RawWindow | None = None,
) -> RmDiagnostics:
    """Pro-Call ΔT-Diagnose (State-Zeile + Lag-Artefakt + optionales Roh-Fenster).

    - **Konditions-Flag** aus ``rows`` (Store m1.3).
    - **ΔT je Etage**: bevorzugt auf dem 10-s-Roh-Fenster (``raw``) mit dem
      zuletzt geschätzten Lag (lag-korrigiert); Fallback = stündlicher
      2-h-Ø (Store). Inaktiv → per_zone bleibt stumm (leer).
    - **Lag je Etage** aus ``lag_art`` (Cache) mit Default-Fallback.
    - **Minderer-Sollspannung** (v0.5-engine-rework): je Etage = Median über
      das Roh-Fenster (a17–19, ``RawWindow.soll``); Fenster fehlt/kurz oder
      Kanal NULL → Default ``config.RM_MINDERER_SOLL_DEFAULT_V`` (10,0 V).

    Deterministisch. ``rows`` = ``State.rows`` (letzte vollständige Stunde).
    """
    cond = rm_condition(rows)
    per_zone: dict[str, dict[str, Any]] = {}
    lag_source: dict[str, str] = {}
    for z in _ZONES:
        lag_s, conf, src = lag_for_zone(lag_art, z)
        lag_source[z] = src
        if raw is not None:
            dt = _dt_raw_lag_corrected(raw, z, lag_s)
            n_raw = int(raw.n)
        else:
            dt = _dt_hourly(rows, z)
            n_raw = 0
        off2 = cond.rm_open_2h.get(z, 0.0)
        if off2 >= _FLOW_OPEN:
            flow = "offen"
        elif off2 <= _FLOW_CLOSED:
            flow = "zu"
        else:
            flow = "flappend"
        # Minderer-Sollspannung (a17–19, statische Vorgabe): 30-min-Median aus
        # dem Roh-Fenster; Fenster fehlt/kurz oder NULL → Default 10,0 V.
        if raw is not None and raw.n >= 2:
            soll_v = raw.soll_v(z)
        else:
            soll_v = config.RM_MINDERER_SOLL_DEFAULT_V
        per_zone[z] = {
            "lag_s": int(lag_s),
            "lag_confidence": round(conf, 3),
            "lag_source": src,
            "delta_t_c": None if dt is None else round(dt, 3),
            "delta_t_target_c": config.RM_DELTA_T_TARGET_C,
            "delta_t_gap_c": None if dt is None else round(config.RM_DELTA_T_TARGET_C - dt, 3),
            "flow_class": flow,
            "rm_open_2h": round(off2, 4),
            "minderer_soll_v": round(soll_v, 3),
            "n_raw_samples": n_raw,
        }
    return RmDiagnostics(
        condition=cond,
        per_zone=per_zone,
        target_c=config.RM_DELTA_T_TARGET_C,
        active=cond.active,
        raw_window_s=int(raw.n * raw.dt_s) if raw is not None else 0,
        lag_source=lag_source,
    )


__all__ = [
    "RmCondition",
    "RmDiagnostics",
    "RawWindow",
    "compute_diagnostics",
    "estimate_lags",
    "lag_for_zone",
    "load_lag_estimate",
    "rm_condition",
]
