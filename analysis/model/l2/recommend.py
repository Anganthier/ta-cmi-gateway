"""L2 v0.3 — Empfehlungse-Engine (deterministisch, Advisory, offline).

v0.3 (maintainer-Korrektur 2026-11-02, FIX 2026-11-03): Komfort-Kosten im
**Ist-Raum** gegen the maintainer's **aktuellen Intent** ``soll = RAS_BASE_C −
offset_now`` (RAS-Standard 20 °C; Ist = Perzipiert − Offset, Inverse von
``apply_offset_step``) statt UVR-Standardkurve ``rt_soll(hour)``. Die
Messlatte ist fix pro Bewertung (kein Kandidaten-Δ) — Details:
docs/l2-v0.3-ist-kalibrierung.md.

Pipeline je Stunde (Design ``docs/ai-model-design.md`` §2.2/§3.2):

1. **Ist-Zustand** aus Feature-Store m1.3 (duckdb, letzte vollständige
   Kalenderstunde ≤ ``ts`` + Store-Datenstand).
2. **LIVE-24-h-Forecast** (Open-Meteo ICON-D2, s. :mod:`l2.weather`);
   API-Fehler → Fallback auf Store-Wetter + ``stale=true`` (kein Crash).
3. **Stale-Guard** (hart, vor Ausgabe): Metrik-Lücke > 1 h ODER Wetter-Run
   > 2 h alt → ``stale=true``, Empfehlung = „beibehalten“.
4. **Kandidaten** (Tier A: RAS-Offset je Zone, 0,5-K-Schritte um Ist-Offset,
   Grid {0, ±0,5, ±1, ±2 K} → Pruning → Cap) + **Guardrails** (hart).
5. **Rollout je Kandidat** gegen L1 v003 (``predict_raw`` auf der Store-Zeile
   mit LIVE-Forecast in die ``wx_t{k}_*``-Spalten injiziert + Offset-Step),
   12-h-Horizont über die direkten Horizonte 1/4/6/12 h (6-h-Zwischencheck).
6. **Kostenfunktion** (Komfort-Band + Energie-Proxy + Änderungs-Strafe) +
   No-change-by-default → beste Aktion.
7. **Output**: :class:`Recommendation` → JSON (Schema Design §3.2, angepasst:
   ``empfehlungen[]`` mit parameter/zone/aktuell/vorgeschlagen/
   wirkung_prognose/begruendung_de/konfidenz/executability) + DE-Begründung
   + Konfidenz (Modell-Skill v003) + Stale + model_version + Datenstand +
   Wetter-Run.

Exkludiert (niemals empfohlen, maintainer-Entscheidung 2026-11-01): Ölbrenner
O5 AN/AUS, Betriebsmodus. v0.5: Minderer-Ist-Vorgabe (Sollspannung) +
Pumpe O8-Status + Heizkurve sind Advisory/Tier-B-Blocks (kein CMI-Write).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from analysis.model.m2b.features import apply_offset_step, vlst_est

from . import config, executability, weather
from . import minderer_curve as minderer_cv
from . import rm_diagnostics as rm_diag
from . import vl_calibration as vl_cal
from . import vl_minimum as vl_min

# --- Rollout (L1 v003): relevante Targets für Kosten + Guardrails + Advisories ---
ROLLOUT_TARGETS: tuple[str, ...] = (
    "ras_kell_perc",
    "ras_eg_perc",
    "ras_dg_perc",
    "oel_burner_on",
    "pump_hz_on",
    "hz_vl",
)
# 12-h-Horizont als Punkt-Menge (0,1,4,6,12 = direkte L1-Horizonte;
# dazwischen lineare Interpolation).
HORIZON_POINTS: tuple[int, ...] = (0, 1, 4, 6, 12)
# Spalten, die ``apply_offset_step`` immer liest (Offset-Semantik v003):
# Offset + perzipiert + perzipiert-last je Zone. Müssen im Rollout-Raw-Dict
# vorhanden sein, auch wenn sie nicht zu den Predictor-``raw_inputs`` gehören.
OFFSET_STEP_COLS: tuple[str, ...] = tuple(
    col for z in config.ZONES for col in (f"offset_{z}", f"ras_{z}_perc", f"ras_{z}_perc_last")
)
# Energieterm-Checkpoints (6-h-Zwischencheck + Horizont-Ende doppelt,
# Design §9: zweite Hälfte unsicherer → höhere Gewichtung).
ENERGY_CHECKPOINTS: dict[int, float] = {1: 0.5, 4: 0.5, 6: 1.0, 12: 2.0}
# Komfort (v0.3): Band ±0,5 K um das abgeleitete RAS-Soll
# (RAS_BASE_C − Offset, maintainer 2026-11-02; NICHT UVR-rt_soll).
COMFORT_BAND_K = 0.5
COMFORT_HOURS: tuple[int, ...] = tuple(range(12))  # t+0 … t+11

# Store-Spalten für den Ist-Zustand (zusätzlich zu Predictor-Rohspalten).
STATE_COLUMNS: tuple[str, ...] = (
    "ts",
    "data_version",
    "gap_full",
    "gap_partial",
    "offset_kell",
    "offset_eg",
    "offset_dg",
    "ras_kell_perc",
    "ras_kell_ist",
    "ras_eg_perc",
    "ras_eg_ist",
    "ras_dg_perc",
    "ras_dg_ist",
    "buff_top",
    "buff_mid",
    "buff_bot",
    "hz_vl",
    "hz_rl",
    "ext",
    "ww",
    "solar_coll",
    "flue",
    "oel_burner_on",
    "oel_burner_last",
    "oel_burner_on_sum12h",
    "oel_burner_on_since_h",
    "pump_hz_on",
    "pump_hz_last",
    "pump_hz_on_sum12h",
    "pump_hz_on_since_h",
    "pump_circ_on",
    "pump_circ_on_sum24h",
    # v0.4: Heizbetrieb-Signal (3-h-Summen; Guard unterdrückt im Ruhezustand
    # die Heizkurven-Empfehlung, s. :func:`heating_active`).
    "oel_burner_on_sum3h",
    "pump_hz_on_sum3h",
    # Effektor-Guard (Fix-2): Rücklaufminderer-Offen-Anteil je Zone
    # (3-h-Fenster aus einer Zeile, s. MINDERER_RULE_COLS).
    "hzrkl_kell_rule",
    "hzrkl_kell_rule_lag1",
    "hzrkl_kell_rule_lag2",
    "hzrkl_eg_rule",
    "hzrkl_eg_rule_lag1",
    "hzrkl_eg_rule_lag2",
    "hzrkl_dg_rule",
    "hzrkl_dg_rule_lag1",
    "hzrkl_dg_rule_lag2",
    # v0.5 Ticket D: ΔT-Diagnose + Lag + Konditions-Flag (s.
    # :mod:`l2.rm_diagnostics`). Minderer-offen-2-h-Fenster (on + lag1),
    # RAS perzipiert + Offset (Intent = RAS_BASE_C − Offset), VL +
    # Strang-RL-Fühler (hzrkl_{z}_pos) für den stündlichen ΔT-Fallback.
    "hzrkl_kell_on",
    "hzrkl_kell_on_lag1",
    "hzrkl_eg_on",
    "hzrkl_eg_on_lag1",
    "hzrkl_dg_on",
    "hzrkl_dg_on_lag1",
    "hzrkl_kell_pos",
    "hzrkl_kell_pos_last",
    "hzrkl_eg_pos",
    "hzrkl_eg_pos_last",
    "hzrkl_dg_pos",
    "hzrkl_dg_pos_last",
    "hz_vl_lag1",
    "wx_t0_t_bias",
    "wx_t12_t_bias",
    "wx_t0_swr",
    "wx_t6_swr",
)
# Effektor-Guard (Fix-2): Minderer-Spalten je Zone (t, t−1, t−2). Fehlend im
# Store (ältere Monate ohne m1.3-Regelspalten) → NULL → „unbekannt“ → Guard
# stumm (Fail-Open).
MINDERER_RULE_COLS: tuple[str, ...] = tuple(
    f"hzrkl_{z}_rule{lag}" for z in config.ZONES for lag in ("", "_lag1", "_lag2")
)


def _f(v: Any) -> float | None:
    """Rohwert → float (NaN/None → None)."""
    if v is None:
        return None
    x = float(v)
    return x if np.isfinite(x) else None


def _h(v: Any) -> float:
    """Rohwert → float (NaN/None → 0.0), für An-Quotienten/Summen."""
    if v is None:
        return 0.0
    x = float(v)
    return x if np.isfinite(x) else 0.0


@dataclass
class State:
    """Ist-Zustand der Entscheidung (letzte vollständige Stunde ≤ ``ts``)."""

    ts_str: str  # 'YYYY-MM-DD HH:00' (naive Berlin, Store-Raster)
    ts: datetime  # aware (Europe/Berlin)
    rows: dict[str, Any]  # Store-Zeile (ts-Zeile bzw. letzte vollständige)
    state_ts_str: str  # tatsächlich genutzte Stunde (vollständig)
    data_version: str
    store_last_ts: str  # letzter Store-Bucket (Datenstand)
    metric_gap_h: float  # Lücke: requested − state (0 wenn requested-Zeile vollständig)
    requested_missing: bool  # Requested-ts-Zeile fehlt im Store
    gap_full: bool  # state-Zeile mit gap_full=1

    def get(self, col: str) -> float | None:
        return _f(self.rows.get(col))

    @property
    def offsets(self) -> dict[str, float]:
        return {z: _h(self.rows.get(f"offset_{z}")) for z in config.ZONES}

    @property
    def perc(self) -> dict[str, float]:
        return {z: _h(self.rows.get(f"ras_{z}_perc")) for z in config.ZONES}

    @property
    def ist(self) -> dict[str, float]:
        """Ist-Raumtemperatur je Zone (``ras_{z}_ist``)."""
        return {z: _h(self.rows.get(f"ras_{z}_ist")) for z in config.ZONES}

    @property
    def soll_now(self) -> dict[str, float]:
        """RAS-Soll je Zone (v0.3): ``RAS_BASE_C − Offset`` (abgeleitet, live)."""
        return {z: config.RAS_BASE_C - _h(self.rows.get(f"offset_{z}")) for z in config.ZONES}


def load_state(
    ts: datetime | str,
    features_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> State:
    """Ist-Zustand + Store-Datenstand aus Feature-Store m1.3 (read-only).

    ``state_ts`` = die Zeile bei ``ts`` (wenn vorhanden und nicht
    ``gap_full``); bei ``gap_full``-Zeile die vorherige vollständige Stunde;
    bei fehlender ts-Zeile die letzte vollständige Bucket-Stunde ≤ ``ts``.
    ``metric_gap_h`` = Lücke zwischen Request und genutzter state-Zeile
    (0 wenn ts-Zeile vollständig vorhanden) — Stale-Größe (Spec: > 1 h →
    stale; ``STORE_STALE_MAX_H`` lässt 0,5 h für den Laufenden-Bucket zu).
    ``now`` wird nicht für Staleness gebraucht (State-Frische ist relativ zur
    Entscheidung, nicht zur Uhr) — bleibt nur für spätere Nutzung (None ok).
    """
    requested = weather.berlin_hour(ts)
    cols = [c for c in STATE_COLUMNS if c not in ("ts", "data_version")]
    cols = list(dict.fromkeys(cols))
    owner = con is None
    con = con or duckdb.connect()
    try:
        glob = str((Path(features_dir) if features_dir else config.FEATURES_DIR) / "*/*/*.parquet")
        row = con.execute(f"SELECT max(ts), max(data_version) FROM '{glob}'").fetchone()
        last_ts, dv = row[0], str(row[1]) if row and row[1] is not None else "unknown"
        last_ts_str = str(last_ts)
        res = con.execute(
            f"SELECT ts, {', '.join(cols)} FROM '{glob}' WHERE ts <= TIMESTAMP '{requested:%Y-%m-%d %H:00}' "
            "ORDER BY ts DESC LIMIT 2"
        )
        names = [d[0] for d in res.description]
        recs = res.fetchall()
    finally:
        if owner and con is not None:
            con.close()
    if not recs:
        raise KeyError(f"Feature-Store enthält keine Zeile <= {requested:%Y-%m-%d %H:00}")
    row_dict = {n: v for n, v in zip(names, recs[0], strict=False)}
    # Zeitstempel-String normalisieren (DuckDB-TIMESTAMP → "… HH:MM:SS"):
    ts_actual = row_dict["ts"]
    ts_actual_str = (
        ts_actual.strftime("%Y-%m-%d %H:00") if hasattr(ts_actual, "strftime") else str(ts_actual)[:16]
    )
    ts_row_missing = ts_actual_str != f"{requested:%Y-%m-%d %H:00}"
    # state-Zeile: ts-Zeile, wenn vorhanden und ohne gap_full; ts-Zeile mit
    # gap_full → vorherige (recs[1]); ts-Zeile fehlt → letzte (recs[0]).
    # Randfall: ts-Zeile mit gap_full IST die älteste Zeile (kein recs[1]) →
    # keine vollständigere Stunde verfügbar → dieselbe Zeile + gap_full-Flag.
    if not ts_row_missing and _h(row_dict.get("gap_full")) == 0.0:
        state_row = row_dict
    elif not ts_row_missing and len(recs) > 1:
        state_row = {n: v for n, v in zip(names, recs[1], strict=False)}
    else:
        state_row = row_dict
    state_ts_str = str(state_row["ts"])
    state_ts = datetime.fromisoformat(state_ts_str).replace(tzinfo=weather.BERLIN)
    metric_gap_h = (requested - state_ts).total_seconds() / 3600.0
    return State(
        ts_str=f"{requested:%Y-%m-%d %H:00}",
        ts=state_ts,
        rows=state_row,
        state_ts_str=state_ts_str,
        data_version=dv,
        store_last_ts=last_ts_str,
        metric_gap_h=metric_gap_h,
        requested_missing=ts_row_missing,
        gap_full=_h(state_row.get("gap_full")) == 1.0,
    )


# --- Kandidaten (Tier A: RAS-Offset je Zone) ---

# Grid ohne Cap (Kandidaten vor der Cap-Auswahl).
ALL_GRID: tuple[dict[str, float], ...] = tuple(
    {"kell": dk, "eg": de, "dg": dd}
    for dk in config.OFFSET_CANDIDATES
    for de in config.OFFSET_CANDIDATES
    for dd in config.OFFSET_CANDIDATES
    if abs(dk) <= config.OFFSET_MAX_DELTA_PER_H
    and abs(de) <= config.OFFSET_MAX_DELTA_PER_H
    and abs(dd) <= config.OFFSET_MAX_DELTA_PER_H
)


def quantize_delta(delta: float) -> float:
    """Offset-Änderung auf 0,5-K-Schritte quantisieren (Guardrail, Design §3.2 R4)."""
    return round(delta * 2.0) / 2.0


def generate_candidates(offsets: dict[str, float], max_candidates: int | None = None) -> list[dict[str, float]]:
    """Offset-Kandidaten (deterministisch: sortiert über ``config.ZONES``).

    Grid: ``{0, ±0,5, ±1, ±2 K}`` Δ um den Ist-Offset je Zone (``ALL_GRID``,
    125 Kombis). Pruning (Raschel-Prävention): ``|Δ| ≤ 1 K`` je Zone, wenn
    ≥ 2 Zonen gleichzeitig nicht 0 sind; Cap in der **Distanz-Reihenfolge**
    (L2-Norm der Δ-Vector, Kopf bleibt) — die Basisaktion (0,0,0) bleibt damit
    immer enthalten. Returns die Cap-Kandidaten (sortiert).
    """
    max_n = max_candidates or config.MAX_CANDIDATES
    cands = [dict(c) for c in ALL_GRID]
    pruned: list[dict[str, float]] = []
    for cand in cands:
        nonzero = sum(1 for d in cand.values() if d != 0.0)
        limit = config.PRUNE_MULTI_MAX_K if nonzero >= 2 else config.PRUNE_SINGLE_MAX_K
        if all(abs(d) <= limit for d in cand.values()):
            pruned.append(cand)
    cands.sort(key=lambda c: (c["kell"], c["eg"], c["dg"]))
    capped = sorted(pruned, key=lambda c: (sum(d * d for d in c.values()), c["kell"], c["eg"], c["dg"]))
    return capped[:max_n]


def candidate_key(cand: dict[str, float]) -> str:
    """Sortierschlüssel-Kennung (für deterministische Reports)."""
    return "+".join(f"{config.ZONES[i]}:{cand[config.ZONES[i]]:+.1f}" for i in range(3))


def minderer_state(rows: dict[str, Any], zone: str) -> tuple[float | None, str]:
    """Rücklaufminderer-Zustand (Fix-2, 3-h-Fenster) aus der Store-Zeile.

    ``rows``: Store-Zeile (``state.rows``). Liest ``hzrkl_{z}_rule`` +
    ``_lag1``/``_lag2`` (Offen-Anteil je Stunde, 3-h-Fenster aus EINER Zeile;
    Spalten s. :data:`MINDERER_RULE_COLS`).

    Returns ``(offen_o, status)``:
    - ``offen_o = None`` = **unbekannt** — eine der 3 Stunden fehlt (NULL);
      der Guard bleibt stumm (Fail-Open: Empfehlungskandidaten unverändert).
    - sonst 3-h-Ø(open) mit Status:

      - ``≤ MINDERER_CLOSED_MAX`` (0,1) = ``„zu"`` — Heizkreis seit ~3 h aus
        (perzipiert > 20,1 °C; Raum wärmer als der Intent): der Offset hat
        keine Stellkraft (RAS heizt, kühlt nicht) → Δ=0-Filter + Hinweis-Text.
      - ``≥ MINDERER_OPEN_MIN`` (0,9) = ``„offen"`` — normal/regelt.
      - sonst = ``„flappend"`` — periodische Aktivierung (nur Advisory-Hinweis,
        wenn der Raum außerhalb des Komfort-Bands liegt; s. :func:`_zone_advice`).
    """
    vals: list[float] = []
    for lag in ("", "_lag1", "_lag2"):
        v = _f(rows.get(f"hzrkl_{zone}_rule{lag}"))
        if v is None:
            return None, "unbekannt"
        vals.append(v)
    off = sum(vals) / config.MINDERER_WINDOW_H
    if off <= config.MINDERER_CLOSED_MAX:
        status = "zu"
    elif off >= config.MINDERER_OPEN_MIN:
        status = "offen"
    else:
        status = "flappend"
    return off, status


def heating_active(rows: dict[str, Any]) -> bool | None:
    """Heizbetrieb-Zustand (v0.4, 3-h-Fenster) aus der Store-Zeile.

    Liest ``oel_burner_on_sum3h`` + ``pump_hz_on_sum3h`` mit :func:`_f`
    (None-safe; **nicht** :func:`_h` — NULL ist unbekannt, kein „aus“).

    Returns:
    - ``None`` = **unbekannt** — eine der beiden Sum-Spalten fehlt (NULL) →
      Guard stumm (Fail-Open: v0.3-Verhalten unverändert).
    - ``True``/``False``: eine der beiden Summen ``≥ VL_HEATING_ACTIVE_EPS``
      (OR-Semantik: Hz-Pumpe an kann ohne Öl-Brenner vorkommen, z. B. rein
      Puffer-Umlauf) → Heizbetrieb aktiv bzw. seit 3 h keiner.
    """
    oel3 = _f(rows.get("oel_burner_on_sum3h"))
    pump3 = _f(rows.get("pump_hz_on_sum3h"))
    if oel3 is None or pump3 is None:
        return None
    return oel3 >= config.VL_HEATING_ACTIVE_EPS or pump3 >= config.VL_HEATING_ACTIVE_EPS


def _features_data_root(features_dir: Path | None) -> Path:
    """Daten-Root aus dem Feature-Store-Pfad (``…/data/features`` → ``…/data``).

    Liegt ``features_dir`` unter ``…/features`` (typischer Store-Pfad), wird
    der Eltern-``data``-Root verwendet; sonst der gegebene Pfad selbst.
    Default (None) = ``config._REPO_ROOT/data``. Im Gateway (``/data/features``)
    = ``/data`` (rw-Bind → Artefakt schreibbar).
    """
    if features_dir is None:
        return config._REPO_ROOT / "data"
    p = Path(features_dir)
    return p.parent if p.name == "features" else p


def load_lag_estimate(
    features_dir: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """Lag-Artefakt (Cache) laden; ``None`` wenn fehlt/ungültig → Default-Lag.

    Pro ``recommend()`` wird das Cache-Artefakt gelesen (kein Roh-Scan). Das
    Artefakt wird einmalig von :func:`rm_diag.estimate_lags` (CLI) geschrieben
    (``data/l2/rm_lag_estimate.json``, im Gateway ``/data/l2/``) —
    deterministisch, kein pro-Call-Neu-Scan der Historie. ``path`` (explizit)
    überschreibt den Daten-Root-Pfad (Tests).
    """
    if path is not None:
        return rm_diag.load_lag_estimate(path)
    return rm_diag.load_lag_estimate(
        rm_diag.lag_estimate_path(_features_data_root(features_dir))
    )


def load_raw_window(
    features_dir: Path | None = None,
    ts: datetime | None = None,
    hours: int | None = None,
) -> rm_diag.RawWindow | None:
    """10-s-Rohdaten-Fenster der letzten ``hours`` h (Default
    ``RM_DT_WINDOW_H`` = 2 h) **bis** ``ts`` (State-Stunde, Default jetzt) für
    die pro-Call ΔT-Diagnose (deterministisch: Fenster-Ende = State-Stunde).

    ``None`` wenn die Rohdaten nicht lesbar sind (→ stündlicher ΔT-Fallback).
    """
    data_dir = _features_data_root(features_dir)
    end = ts if ts is not None else datetime.now(weather.BERLIN)
    window_h = hours if hours is not None else config.RM_DT_WINDOW_H
    since = (end - timedelta(hours=window_h)).strftime("%Y-%m-%d %H:%M:%S")
    until = end.strftime("%Y-%m-%d %H:%M:%S")
    return rm_diag._read_raw_since(data_dir, since, until)


def load_vl_calibration(
    features_dir: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """VL-Kalibrierungs-Artefakt (Cache, Ticket C) laden; ``None`` wenn
    fehlt/ungültig → v0.4-Heuristik-Fallback + „Kalibrierung nicht
    verfügbar“-Hinweis.

    Pro ``recommend()`` wird das Cache-Artefakt gelesen (kein Historie-Scan).
    Das Artefakt wird **einmalig** von :func:`vl_cal.calibrate_vl` (CLI
    ``python -m analysis.model.l2 calibrate-vl``) geschrieben
    (``data/l2/vl_kalibrierung.json``, im Gateway ``/data/l2/``) — Trennung
    dokumentiert (Rebuild-Befehl im Artefakt + Doku). ``path`` (explizit)
    überschreibt den Daten-Root-Pfad (Tests).
    """
    if path is not None:
        return vl_cal.load_vl_calibration(path)
    return vl_cal.load_vl_calibration(
        vl_cal.vl_calibration_path(_features_data_root(features_dir))
    )


def load_vl_minimum(
    features_dir: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """VL-Minimum-Artefakt (Cache, v0.6-B) laden; ``None`` wenn
    fehlt/ungültig → der datenbasierte Kaskaden-Zweig entfällt
    (v0.5-Standardkurve-Verhalten bleibt exakt, s.
    :func:`_vl_curve_advice`).

    Pro ``recommend()`` wird das Cache-Artefakt gelesen (kein
    Historie-Scan). Das Artefakt wird **einmalig** von
    :func:`vl_min.calibrate_vl_min` (CLI ``python -m analysis.model.l2
    calibrate-vl-min``) geschrieben (``data/l2/vl_minimum.json``, im
    Gateway ``/data/l2/``) — Trennung dokumentiert (Rebuild-Befehl im
    Artefakt + Doku). ``path`` (explizit) überschreibt den
    Daten-Root-Pfad (Tests).
    """
    if path is not None:
        return vl_min.load_vl_minimum(path)
    return vl_min.load_vl_minimum(vl_min.vl_minimum_path(_features_data_root(features_dir)))


def load_minderer_curve(
    features_dir: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """Minderer-V→ΔT-Kurven-Artefakt (Cache, v0.6-A) laden; ``None`` wenn
    fehlt/ungültig → linear-Fallback (v0.5-Verhalten bleibt exakt, s.
    :func:`minderer_v_target_v` + :func:`_minderer_advisories`).

    Pro ``recommend()`` wird das Cache-Artefakt gelesen (kein
    Historie-Scan). Das Artefakt wird **einmalig** von
    :func:`minderer_cv.build_minderer_curve` (CLI ``python -m
    analysis.model.l2 calibrate-minderer-curve``) geschrieben
    (``data/l2/minderer_curve.json``, im Gateway ``/data/l2/``) — Trennung
    dokumentiert (Rebuild-Befehl im Artefakt + Doku). ``path`` (explizit)
    überschreibt den Daten-Root-Pfad (Tests).
    """
    if path is not None:
        return minderer_cv.load_minderer_curve(path)
    return minderer_cv.load_minderer_curve(
        minderer_cv.minderer_curve_path(_features_data_root(features_dir))
    )


# --- Kosten ---


@dataclass
class CostBreakdown:
    """Kosten je Kandidat (Design §3.1, v0.1-Default-Gewichte in config)."""

    comfort: float
    energy: float
    change: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return (
            config.W_COMFORT * self.comfort
            + config.W_ENERGY * self.energy
            + config.W_CHANGE * self.change
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "comfort": round(self.comfort, 4),
            "energy": round(self.energy, 4),
            "change": round(self.change, 4),
            "total": round(self.total, 4),
        }


def _interp_traj(values: dict[int, float]) -> dict[int, float]:
    """Punkte {0,1,4,6,12} → Stundenwerte {0..12} (linear zwischen Stützpunkten)."""
    out: dict[int, float] = {}
    segs = ((0, 1), (1, 4), (4, 6), (6, 12))
    for h in range(13):
        if h in values:
            out[h] = values[h]
            continue
        for lo, hi in segs:
            if lo < h < hi:
                w = (h - lo) / (hi - lo)
                out[h] = values[lo] + w * (values[hi] - values[lo])
                break
    return out


def ras_soll_now(off_now: float) -> float:
    """Komfort-Referenz (v0.3-FIX): the maintainer's **aktueller Intent** ``20 − off_now``.

    Fix für die gesamte Kandidaten-Bewertung (KEIN Δ): ein Kandidat verschiebt
    sein eigenes Ziel nur über die L1-Perzipiert-Dynamik; die Messlatte bleibt
    the maintainer's Einstellung (Offset jetzt, nicht das Kandidaten-Ziel).
    """
    return config.RAS_BASE_C - off_now


def comfort_cost(
    perz_traj: dict[int, dict[int, float]],
    offsets_now: dict[str, float],
    cand: dict[str, float],
) -> float:
    """Komfort (v0.3-FIX, Ist-Raum): Σ_Zonen Σ_{h=0..11}
    max(0, |ist_cand_z(t+h) − intent_z| − Band)².

    ``ist_cand = perz_cand − cand_offset`` (exakte Inverse von
    ``apply_offset_step``; h=0 = ungestörter Ist-Wert ``perz_now − off_now``)
    und ``intent = ras_soll_now(off_now) = RAS_BASE_C − off_now`` — the maintainer's
    aktueller Intent, **fix** für alle Kandidaten (KEIN Δ).

    Abgrenzung v0.3-Alpha (verworfen): dort war die Referenz das
    **Kandidaten-eigene Ziel** ``RAS_BASE_C − (off_now + Δ)`` → (1) zirkulär:
    jeder Kandidat misst seinen Komfort an seinem eigenen Ziel; (2) h=0-
    Buchführungs-Artefakt: ``|perz_now + Δ − 20|`` belohnt Δ<0 im warmen Raum,
    obwohl der Raum sich physikalisch nicht bewegt hat (Keller-Beleg 20,5@16,5
    → „wärmer machen"). H=0-Identität: Abweichung bei h=0 = |ist_now −
    intent| für ALLE Δ (identisch); die Kandidaten-Abhängigkeit läuft NUR über
    das L1-Offset-konditionierte Perzipiert-Transient (gewollt).
    ``perz_traj``: Perzipiert-Trajektorie aus dem Rollout ({z: {h: Wert}},
    Stützpunkte HORIZON_POINTS).
    """
    total = 0.0
    for z, traj in perz_traj.items():
        cand_off = offsets_now[z] + cand[z]
        intent = ras_soll_now(offsets_now[z])  # fix: the maintainer's aktueller Intent
        full = _interp_traj(traj)
        for h in COMFORT_HOURS:
            ist_cand = full[h] - cand_off  # Inverse von apply_offset_step
            dev = abs(ist_cand - intent) - COMFORT_BAND_K
            total += max(0.0, dev) ** 2
    return total


def energy_cost(rollout: dict[int, dict[str, float]]) -> float:
    """Energie-Proxy: Öl-An-Quotient + (VL−Ref)·Pumpen-An-Quotient, gecheckpointed.

    Proxy statt Liter (Q-106-Öl-CSV blockiert M7): 1,0 ≈ 1 h Brenner-Vollan;
    VL-Term skaliert mit ``VL_PROXY_SCALE`` (Design: Konfidenz-Band).
    """
    e = 0.0
    for h, w in ENERGY_CHECKPOINTS.items():
        oil = _h(rollout.get(h, {}).get("oel_burner_on"))
        vl = _h(rollout.get(h, {}).get("hz_vl"))
        pump = _h(rollout.get(h, {}).get("pump_hz_on"))
        e += w * (oil + max(0.0, vl - config.VL_REF_C) / config.VL_PROXY_SCALE * pump)
    return e


def change_cost(cand: dict[str, float]) -> float:
    """Änderungs-Strafe: Σ 1[Δ≠0] · 0,5·|ΔK| je Zone (Design §2.3)."""
    return 0.5 * sum(abs(d) for d in cand.values() if d != 0.0)


# --- Guardrails (hart, vor Ausgabe) ---


def apply_guardrails(
    cands: list[dict[str, float]],
    state: State,
    wx: weather.WxLive | None,
) -> tuple[list[dict[str, float]], list[str]]:
    """Harte Guardrails (Design §3.2 R4 + L2 v0.1): Violation = Kandidat verworfen + geloggt.

    - Offset-Änderung ≤ ±2 K/Stunde + 0,5-K-Quantisierung (Grid-Guarantie, explizit geprüft).
    - Puffer > 70 °C + Lade-Anforderung (Brenner/Pumpe an) → keine Lade-Empfehlung
      (keine Offset-Senkung in Zone; ohne Lade-Anforderung = nichts zu laden).
    - Frost (12 h) + Puffer < 40 °C → Offset nie senken (Wärmeentzug).
    - Kollektor < 25 °C / Puffer-Gates wirken zusätzlich auf Tier B (s. Advisories).
    """
    hits: list[str] = []
    buff_top = _h(state.rows.get("buff_top"))
    frost = bool(wx and weather.wx_frost_night(wx))
    buff_low = buff_top is not None and buff_top < config.FROST_BUFF_MIN_C
    frozen = frost and buff_low
    loading = _h(state.rows.get("oel_burner_on")) > 0.05 or _h(state.rows.get("pump_hz_on")) > 0.05
    overfull_loading = buff_top is not None and buff_top > config.BUFF_TOP_MAX_C and loading
    kept: list[dict[str, float]] = []
    for cand in cands:
        ok = True
        for z in config.ZONES:
            d = quantize_delta(cand[z])
            cand[z] = d
            if abs(d) > config.OFFSET_MAX_DELTA_PER_H + 1e-9:
                ok = False
                hits.append(f"offset {z} Δ{d:+.1f} K > ±{config.OFFSET_MAX_DELTA_PER_H:.0f} K/h (verworfen)")
            if overfull_loading and d < 0:
                ok = False
                hits.append(
                    f"offset {z} Δ{d:+.1f} K: Puffer {buff_top:.0f} °C > {config.BUFF_TOP_MAX_C:.0f} °C "
                    f"+ Lade-Anforderung → keine Lade-Empfehlung (verworfen)"
                )
            if frozen and d < 0:
                ok = False
                hits.append(
                    f"offset {z} Δ{d:+.1f} K: Frost + Puffer {buff_top:.0f} °C < {config.FROST_BUFF_MIN_C:.0f} °C → Offset nicht senken (verworfen)"
                )
        if ok:
            kept.append(cand)
    return kept, hits


# --- Rollout (L1 v003) ---


def inject_live_wx(raw: dict[str, np.ndarray], wx: weather.WxLive) -> dict[str, np.ndarray]:
    """LIVE-Forecast in die ``wx_t{k}_*``-Rohspalten der Store-Zeile injizieren.

    Mapping 1:1 zum M1-Feature-Build: ``wx_t{k}`` = Grid-Stunde ``ts_utc + k h``
    (s. M1-``assemble_features``: ``w{k}.hour_utc = g.hour_utc + INTERVAL k``).
    ``t_bias`` = Grid-T + M1-Bias-Korrektur (CMI-Außensensor); die übrigen
    Variablen Roh-Grid-Werte. Fehlende Spalten (kleinere Test-Stores) bleiben.
    """
    out = dict(raw)
    shorts = {
        "temperature_2m": "t_bias",
        "dew_point_2m": "dew",
        "relative_humidity_2m": "rh",
        "cloudcover": "cc",
        "cloudcover_low": "cc_low",
        "cloudcover_mid": "cc_mid",
        "cloudcover_high": "cc_high",
        "precipitation": "prec",
        "rain": "rain",
        "wind_speed_10m": "ws",
        "wind_gusts_10m": "wg",
        "wind_direction_10m": "wd",
        "shortwave_radiation": "swr",
        "weather_code": "wmo",
    }
    for k in range(config.WX_HORIZON_H):
        for var, short in shorts.items():
            col = f"wx_t{k}_{short}"
            if col not in out:
                continue
            vals = wx.hourly.get(var, [None] * len(wx.utc_hour))
            v = vals[k] if k < len(vals) else None
            if short == "t_bias":
                v = wx.t_bias[k] if k < len(wx.t_bias) else None
            out[col] = np.array([None if v is None else float(v)], dtype=np.float64)
    return out


def rollout(
    predictor: Any,
    ts_str: str,
    raw: dict[str, np.ndarray],
    tsa: np.ndarray,
    cand: dict[str, float],
) -> dict[int, dict[str, float]]:
    """1-Kandidaten-Rollout: Offset-Step + ``predict_raw`` → {h: {target: Wert}}.

    Semantik wie ``L1Predictor.predict_at`` (v002-Ansatz): Aktion (Offset) der
    Stunde t gilt über den Horizont als gehalten; h=0 = geänderter Ist-Zustand.
    """
    r = dict(raw)
    for z in config.ZONES:
        d = quantize_delta(cand[z])
        if d != 0.0:
            r = apply_offset_step(r, z, float(d))
    res = predictor.predict_raw(r, tsa, targets=list(ROLLOUT_TARGETS))
    out: dict[int, dict[str, float]] = {}
    for (t, h), v in res.items():
        out.setdefault(int(h), {})[t] = float(v[0])
    # h=0 = geänderter Ist-Zustand (keine Modell-Abweichung; wie
    # ``L1Predictor.predict_at``: h=0 = Rohwert, **Target-Namen**).
    out[0] = {f"ras_{z}_perc": float(r[f"ras_{z}_perc"][0]) for z in config.ZONES}
    for sig in ("oel_burner_on", "pump_hz_on", "hz_vl"):
        out[0][sig] = float(r[sig][0]) if sig in r else 0.0
    return out


def candidate_cost(
    rollout: dict[int, dict[str, float]],
    cand: dict[str, float],
    offsets_now: dict[str, float],
) -> CostBreakdown:
    """Kosten je Kandidat aus Rollout + Zustand (Komfort/Energie/Change).

    Komfort (v0.3-FIX): Ist-Rekonstruktion aus dem Rollout (``perz −
    cand_offset``) gegen den fixen Intent ``RAS_BASE_C − off_now`` (kein Δ,
    s. :func:`comfort_cost`).
    """
    perc_traj = {z: {h: rollout.get(h, {}).get(f"ras_{z}_perc", float("nan")) for h in HORIZON_POINTS}
                 for z in config.ZONES}
    return CostBreakdown(
        comfort=comfort_cost(perc_traj, offsets_now, cand),
        energy=energy_cost(rollout),
        change=change_cost(cand),
        detail={"rollout": {h: {k: round(v, 3) for k, v in d.items()} for h, d in sorted(rollout.items())}},
    )


# --- Konfidenz (Modell-Skill des relevanten Targets, v003 summary.json) ---


def load_skill_map(artifact_dir: Path | None = None) -> dict[tuple[str, int], float]:
    """Skill-Map {(target, h): skill_b0} aus ``summary.json`` (v003).

    Variante-Priorität: ``lgbm_refit`` (deploytes Artefakt, Split 'test') →
    ``lgbm`` (Split 'valid'); erster endlicher Wert pro (target, h).
    """
    d = Path(artifact_dir) if artifact_dir else config.ARTIFACT_DIR
    p = d / "summary.json"
    if not p.exists():
        return {}
    try:
        s = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[tuple[str, int], float] = {}
    metrics = s.get("metrics") or {}
    for variant in ("lgbm_refit", "lgbm"):
        for target_h, splits in (metrics.get(variant) or {}).items():
            parts = target_h.rsplit("@", 1)
            if len(parts) != 2:
                continue
            t, h = parts[0], int(parts[1])
            if (t, h) in out:
                continue
            for split in ("test", "valid"):
                m = (splits or {}).get(split) or {}
                sk = m.get("skill_b0")
                if sk is not None and np.isfinite(sk):
                    out[(t, h)] = float(sk)
                    break
    return out


def base_confidence(skill_map: dict[tuple[str, int], float]) -> float:
    """Gewichtete Basis-Konfidenz (Skill der 12-h-Relevanz-Targets, v0.1-Default)."""
    num = 0.0
    den = 0.0
    for target, w in config.CONF_WEIGHTS.items():
        sk = skill_map.get((target, 12), skill_map.get((target, 6), skill_map.get((target, 4))))
        if sk is None:
            continue
        num += w * sk
        den += w
    if den == 0.0:
        return config.CONF_FLOOR
    return min(max(num / den, config.CONF_FLOOR), config.CONF_CAP)


# --- Tier B: regelnbasierte Tages-Advisories (KEIN Rollout) ---


def tier_b_advisories(
    state: State,
    wx: weather.WxLive | None,
    guardrail_hits: list[str],
    discovery: Any = None,
    rm_diag: dict[str, Any] | None = None,
    vl_calib: dict[str, Any] | None = None,
    vl_min_art: dict[str, Any] | None = None,
    minderer_curve: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Heizkurve/VL-Soll + WW + Zirkulation + (v0.5) Minderer + Pumpe O8.

    Advisory-Blocks (Ölbrenner nie direkt). ``discovery``: Discovery-Artefakt
    für die v0.2-Executability-Markierung (einmal pro ``recommend()``
    geladen, hier injiziert); ``None`` = Fail-Closed (kuratierte
    Advisory-Labels, nie ``cmi-settable``).

    v0.5: ``rm_diag`` (Ticket-D-ΔT-Diagnose, ``RmDiagnostics.to_dict()``)
    steuert die Gating-Flags für die neuen Minderer- (A) + Pumpen-
    (B)-Blocks (inaktiv → stumm); ``vl_calib`` (Ticket-C-Kalibrierungs-
    Artefakt) ersetzt im Heizkurven-Block das methodisch falsche
    „0,1-K-Diff-Prinzip“ durch die VL-Soll-Kalibrierung (Heizstunden-Only ×
    Außen-T-Bänder; Cache-Read).

    v0.6-B: ``vl_min_art`` (VL-Minimum-Artefakt, Cache) + die live
    Ist-Zustands-Zeile (``state``) steuern die Heizkurven-Kaskade
    (Recover → datenbasiert → v0.5-Standardkurve-Fallback, s.
    :func:`_vl_curve_advice`); ``None`` (Cache fehlt) = exakt das
    v0.5-Verhalten (Datenmangel = Fallback unverändert).

    v0.6-A: ``minderer_curve`` (Minderer-V→ΔT-Kurven-Artefakt, Cache) →
    V-Ziel im Minderer-Block (A) aus der datenbasierten Kennlinie
    (2-Faktor, last-normalisiert; Inverse + Guards); ``None`` (Cache
    fehlt / Zone nicht usable) = exakt das v0.5-lineare Verhalten
    (Regression, s. :func:`_minderer_advisories`).
    """
    out: list[dict[str, Any]] = []
    buff_top = _h(state.rows.get("buff_top")) or 0.0
    solar_coll = _h(state.rows.get("solar_coll"))
    # --- (1) Heizkurve / VL-Soll (v0.5 Ticket C: VL-Soll-Kalibrierung;
    # v0.6-B: Kaskade Recover → datenbasiert → v0.5-Standardkurve) ---
    # v0.4-Guard (unverändert): kein Heizbetrieb seit 3 h → keine
    # Absatz-Empfehlung, Ruhezustand-Text (s. :func:`heating_active`).
    # Neu (Ticket C): statt VL-vs-``hz_rl``-jetzt (0,1-K-Diff-Prinzip,
    # methodisch falsch — VL↔RL-Lag pro Etage + durchflussabhängig, Q-124):
    # **tatsächlicher VL-Ø** × **Heizstunden-Only** × **Außen-T-Bänder (3 K)**
    # gegen die UVR-Standard-Heizkurve (``vlst_est``/``HK_CURVE``). VL ist
    # global → pro-Etage-Lag-Korrektur hier nicht nötig. Cache fehlt →
    # v0.4-Heuristik + „Kalibrierung nicht verfügbar“.
    # v0.6-B (Kaskade, s. :func:`_vl_curve_advice`): (1) **Recover** live
    # (Raum ≥ 1,5 K unter Soll → Kurve +N Raster temporär, kein Cache),
    # (2) **datenbasiert** (VL-Minimum-Cache je Außen-T-Band, Cache-Read),
    # (3) **v0.5-Fallback** (Standardkurve-Abweichung + Heuristik, unverändert
    # bei Datenmangel).
    ext_now = _f(state.rows.get("ext"))
    hz_vl_now = _f(state.rows.get("hz_vl"))
    hz_rl_now = _f(state.rows.get("hz_rl"))  # nur für den v0.4-Ruhezustand-Text
    if ext_now is not None and hz_vl_now is not None:
        curve_now = float(vlst_est(np.array([ext_now]))[0])
        # v0.4-Ruhezustand-Text bleibt identisch: Abweichung = VL-RL (hz_rl)
        # vs Kurve (bzw. hz_vl, wenn hz_rl NULL ist).
        err_now = (hz_rl_now if hz_rl_now is not None else hz_vl_now) - curve_now
        # v0.4: Betriebsfenster-Guard (Heizbetrieb in den letzten 3 h). Kein
        # Heizbetrieb (Öl-Brenner + Hz-Pumpe aus) → das VL-Soll hat keine
        # Stellkraft (UVR folgt dem VL-Soll nur, wenn geheizt wird) → keine
        # Absatz-Empfehlung (Ruhezustand: VL kühlt nur passiv ab).
        # NULL → unbekannt → Guard stumm (Fail-Open, v0.3-Verhalten).
        aktiv = heating_active(state.rows)
        if aktiv is False:
            oel3 = _f(state.rows.get("oel_burner_on_sum3h"))
            pump3 = _f(state.rows.get("pump_hz_on_sum3h"))
            guardrail_hits.append(
                f"vl: kein Heizbetrieb (3-h-Summen {oel3:.2f}/{pump3:.2f}) "
                "→ Kurven-Empfehlung unterdrückt"
            )
            vorgeschlagen = "beibehalten (kein Heizbetrieb)"
            begruendung = (
                f"Kein Heizbetrieb seit 3 h (Öl-Brenner + Heizkreispumpe aus) — "
                f"Vorlauf kühlt passiv ab (Puffer {buff_top:.0f} °C); "
                f"Kurven-Abweichung {err_now:+.1f} K ohne Bedeutung "
                "(VL-Soll wirkt nur im Heizbetrieb; z. B. Kamin hält Puffer)."
            )
            out.append(
                {
                    "parameter": "Heizkurve/VL-Soll",
                    "zone": None,
                    "aktuell": f"VL-RL {hz_vl_now:.1f} °C bei Außen {ext_now:.1f} °C (Kurve-Abweichung {err_now:+.1f} K)",
                    "vorgeschlagen": vorgeschlagen,
                    "wirkung_prognose": "keine Änderung (Ruhezustand, VL-Soll ohne Stellkraft)",
                    "begruendung_de": begruendung,
                    "konfidenz": 0.5,
                    **_exec("vl_curve", None, discovery),
                    "tier": "B",
                }
            )
        else:
            out.append(
                _vl_curve_advice(state, ext_now, hz_vl_now, curve_now, vl_calib, discovery,
                                 vl_min_art)
            )
    # --- (2) WW (Warmwasser) ---
    ww_margin = 57.0 - buff_top
    solar_now = wx is not None and weather.wx_solar_12h(wx) is not None and weather.wx_solar_12h(wx) >= config.WW_SWR_MIN_WH_M2
    coll_ok = solar_coll is not None and solar_coll >= config.SOLAR_COLL_MIN_C
    if ww_margin > 0 and buff_top <= config.BUFF_TOP_MAX_C:
        if coll_ok:
            basis = "Solar-/Holz-Vorfüll-Fenster (Strahlungs-Summe 12 h ≥ 1500 Wh/m²)" if solar_now else "Holz-Vorfüll-Fenster"
            txt = f"WW in {basis} erzeugen, Ölbrenner-Start in milde Fenster schieben (WW-Soll {57.0:.0f} °C, Puffer-Oben {buff_top:.1f} °C)"
        else:
            txt = f"WW-Fenster: Puffer-Oben {buff_top:.1f} °C < 57 °C (Marge {ww_margin:.1f} K); Kollektor {solar_coll:.0f} °C — Solar-Vorfüll erst ab 25 °C, sonst milde Ölfenster nutzen"
        out.append(
            {
                "parameter": "WW-Ladefenster",
                "zone": None,
                "aktuell": f"Puffer-Oben {buff_top:.1f} °C (WW-Marge {ww_margin:.1f} K)",
                "vorgeschlagen": txt,
                "wirkung_prognose": "WW-Erzeugung im günstigen Erzeuger-Fenster (kein Öl-Start bei Kälte)",
                "begruendung_de": (
                    f"uvr_ww_margin = 57 − Puffer-Oben = {ww_margin:.1f} K > 0: WW-Anforderung aktiv. "
                    f"Regelnbasiert (kein Rollout): {txt}. Achtung: Ölbrenner AN/AUS wird NIE direkt "
                    "empfohlen — nur über Fenster/Parameter beeinflusst."
                ),
                "konfidenz": 0.5,
                **_exec("ww_window", None, discovery),
                "tier": "B",
            }
        )
    elif solar_coll is not None and solar_coll < config.SOLAR_COLL_MIN_C:
        guardrail_hits.append(f"solar: Kollektor {solar_coll:.0f} °C < {config.SOLAR_COLL_MIN_C:.0f} °C → keine Solar-Hand-EIN")
    # --- (3) Zirkulation ---
    circ_24h = _h(state.rows.get("pump_circ_on_sum24h"))
    if circ_24h >= config.CIRC_ON_24H_MIN and buff_top >= config.CIRC_BUFF_MIN_C:
        out.append(
            {
                "parameter": "Zirkulation",
                "zone": None,
                "aktuell": f"Zirkulationspumpe {circ_24h:.0f} h/24 h an, Puffer {buff_top:.0f} °C",
                "vorgeschlagen": "Zeitfenster/Nachlauf prüfen (Puffer-geführt → nutzergeführt)",
                "wirkung_prognose": "Weniger Vorlauf-Pendel bei gleichem Komfort (Puffer hält das Niveau)",
                "begruendung_de": (
                    f"Zirkulation war in 24 h {circ_24h:.0f} h aktiv bei Puffer {buff_top:.0f} °C. "
                    "Hinweis: echte „nach Nutzungsverhalten statt Puffer-geführte Zirkulation“ ist eine "
                    "UVR-Programmier-Aufgabe (kein Live-Parameter) — Advisory bleibt regelnbasiert."
                ),
                "konfidenz": 0.4,
                **_exec("circulation", None, discovery),
                "tier": "B",
            }
        )
    # --- (4) v0.5 Ticket A: Minderer-Empfehlung (Advisory, je aktivem Kreis) ---
    out.extend(_minderer_advisories(state, rm_diag, discovery, minderer_curve))
    # --- (5) v0.5 Ticket B: Pumpen-Empfehlung O8 (Advisory, Gating Kondition) ---
    out.extend(_pump_o8_advisories(rm_diag, discovery))
    return out


# --- v0.5 Ticket C: VL-Soll-Kalibrierung (Heizkurven-Block, aktiv) ----------


def _vl_curve_advice(
    state: State,
    ext_now: float,
    hz_vl_now: float,
    curve_now: float,
    vl_calib: dict[str, Any] | None,
    discovery: Any,
    vl_min_art: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Heizkurven-Block (aktiv) — Kaskade (v0.6-B, maintainer-Zielfunktion
    „VL so niedrig wie möglich, Räume halten trotzdem die Temperatur“):

    1. **Recover (live, kein Cache)**: Raum aktuell ≥
       ``VL_MIN_RECOVER_GAP_K`` (1,5 K) unter seinem RAS-Soll
       (``RAS_BASE_C − offset``, v0.3-Ableitung wie im RAS-Offset-Block)
       → „Kurven-Absatz +N Raster (temporär, bis Raum am Soll, dann
       zurück)“; ``N = min(2, ceil((|dev| − 1,5 K)/VL_RASTER_K))``
       (:func:`vl_min.recover_n_raster`). OVERRIDE nur bei
       ``dev ≤ −1,5 K``; sonst Kaskade-2/3. Klartext-Hinweis: die UVR610
       regelt die Raum-T selbst aktiv (reaktiv) → L2 ergänzt nur
       antizipativ, zählt das NICHT doppelt.
    2. **Datenbasiert (Cache)**: usables VL-Minimum-Band (``n_am_soll ≥
       VL_MIN_MIN_N``) für die IST-Außen-T (3 K, konsistent mit
       :mod:`l2.vl_calibration`) → Empfehlung gegen das Mindest-VL-Soll
       (statt Standardkurve): Ist-VL unter dem Minimum → +N Raster
       (anheben, Komfort-Floor); deutlich darüber → −1 Raster (absenken,
       „Kurve bis ~Y °C absenkbar“); dazwischen → beibehalten.
    3. **Fallback (v0.5, bei Datenmangel unverändert)**: Standardkurve-
       Abweichung + Heuristik — exakt das v0.5-Verhalten
       (``vl_cal.curve_deviation_at`` + ``raster_sign``/v0.4-Schwellen;
       „kein Anheben“ nur mit usables Kalibrierungs-Band).

    Ölbrenner/VL-Soll selbst NIE direkt empfohlen;
    ``executability = advisory-ta-designer`` (Q-108). Präsentation:
    **Klartext in ``aktuell``/``vorgeschlagen``** (Ist-VL bei Außen-T),
    Jargon (Bänder, Quantile, n, Lernstart, Raster-Kalibrierung) →
    **Detail-Feld** (UI: klein/muted, einklappbar).
    """
    # --- Kaskade-Stufe 1: Recover (live, kein Cache) ---------------------
    # Raum-Abweichung = ras_{z}_ist − (RAS_BASE_C − offset_{z}) (v0.3-
    # Ableitung, wie im RAS-Offset-Block); nur geloggte Zonen (Ist ≠
    # NULL) zählen. Trigger: min-Abweichung ≤ −VL_MIN_RECOVER_GAP_K.
    devs = {
        z: ist - (config.RAS_BASE_C - _h(state.rows.get(f"offset_{z}")))
        for z, ist in state.ist.items()
        if _f(state.rows.get(f"ras_{z}_ist")) is not None
    }
    worst = min(devs.items(), key=lambda kv: kv[1]) if devs else None
    rec_n = 0
    if worst is not None and worst[1] <= -config.VL_MIN_RECOVER_GAP_K:
        z, dev = worst
        rec_n = vl_min.recover_n_raster(dev)
        vorgeschlagen = (
            f"Kurven-Absatz +{rec_n} Raster (temporär, bis Raum am Soll, dann zurück): "
            f"Raum {z} {abs(dev):.1f} K unter Soll — Kurve kurzfristig höher, "
            "die UVR610 regelt die Raum-T reaktiv nach (ABSENK/NORMAL + RAS); "
            "Sobald der Raum am Soll ist, Absatz wieder zurücksetzen."
        )
        wirkung = (
            f"VL-Soll steigt temporär um ~{rec_n * config.VL_RASTER_K:.0f} K "
            f"(Ziel-VL ~{hz_vl_now + rec_n * config.VL_RASTER_K:.1f} °C bei Außen "
            f"{ext_now:.1f} °C), bis Raum {z} am Soll"
        )
        detail = (
            f"Recover (live, kein Cache): Raum {z} {dev:.1f} K unter Soll "
            f"(Ist {state.ist[z]:.1f} °C vs Soll "
            f"{config.RAS_BASE_C - _h(state.rows.get(f'offset_{z}')):.1f} °C) → "
            f"+{rec_n} Raster temporär (N = min(max(1, ceil((|dev| − "
            f"{config.VL_MIN_RECOVER_GAP_K:.1f} K)/VL_RASTER_K = "
            f"{config.VL_RASTER_K:.1f} K))), Klemme {config.VL_MIN_RECOVER_MAX_RASTER}). "
            "Klartext: die UVR610 regelt die Raum-T selbst aktiv (reaktiv) — "
            "L2 ergänzt nur antizipativ und zählt das nicht doppelt. Datenbasis: "
            "keine (Recover = Ist-Zustand). "
            "Ölbrenner/Vorlaufsoll selbst werden NIE direkt empfohlen."
        )
        return {
            "parameter": "Heizkurve/VL-Soll",
            "zone": None,
            "aktuell": (
                f"Ist-VL {hz_vl_now:.1f} °C bei Außen {ext_now:.1f} °C "
                f"(Raum {z} {dev:.1f} K unter Soll)"
            ),
            "vorgeschlagen": vorgeschlagen,
            "wirkung_prognose": wirkung,
            "begruendung_de": detail,
            "detail": detail,
            "konfidenz": 0.5,
            **_exec("vl_curve", None, discovery),
            "tier": "B",
        }

    # --- Kaskade-Stufe 2: datenbasiert (Cache: VL-Minimum je Band) -------
    band = vl_min.band_at(vl_min_art, ext_now) if vl_min_art else None
    if band is not None:
        vl_min_soll = float(band["vl_min_soll"])
        n = int(band.get("n_am_soll", 0))
        basis = (
            f"Datenbasiert (n={n} Heizstunden ab {vl_min.LEARN_START_SHORT}: "
            f"Kurve im Außen-T-Band {band.get('band_lo'):.0f}…"
            f"{band.get('band_hi'):.0f} °C bis ~{vl_min_soll:.1f} °C absenkbar, bei "
            f"dem die Räume am Soll blieben; Cache-Artefakt, Rebuild "
            f"`calibrate-vl-min`)"
        )
        gap = hz_vl_now - vl_min_soll
        if gap < -config.VL_RASTER_K:
            raster = 1  # deutlich unter dem Minimum → anheben (Komfort-Floor)
        elif gap > 3 * config.VL_RASTER_K:
            raster = -1  # deutlich über dem Minimum → absenken
        else:
            raster = 0
        ziel = vl_min_soll + raster * config.VL_RASTER_K
        if raster < 0:
            vorgeschlagen = (
                "Heizkurven-Absatz −1 Raster: 1 Raster absenken → ~0,5–1 K weniger "
                f"VL-Soll (etwas weniger Öl), ohne Komfortverlust. Ziel-VL ~{ziel:.1f} °C. "
                "Manuell im UVR/TA-Designer."
            )
            wirkung = f"VL-Soll sinkt um ~0,5–1 K (Ziel-VL ~{ziel:.1f} °C bei Außen {ext_now:.1f} °C)"
        elif raster > 0:
            vorgeschlagen = (
                "Heizkurven-Absatz +1 Raster: 1 Raster anheben → ~0,5–1 K mehr "
                f"VL-Soll (mehr Wärme). Ziel-VL ~{ziel:.1f} °C. "
                "Manuell im UVR/TA-Designer."
            )
            wirkung = f"VL-Soll steigt um ~0,5–1 K (Ziel-VL ~{ziel:.1f} °C bei Außen {ext_now:.1f} °C)"
        else:
            vorgeschlagen = "Heizkurve beibehalten (VL im Daten-Band, Räume am Soll)"
            wirkung = "keine Änderung (VL-Soll im Daten-Band)"
        if gap > 0.05:
            abweich_txt = f"VL ~{gap:.1f} K zu hoch"
        elif gap < -0.05:
            abweich_txt = f"VL ~{abs(gap):.1f} K zu niedrig"
        else:
            abweich_txt = "VL im Daten-Band"
        detail = (
            f"Raster-/Kalibrierung: Ist-VL {hz_vl_now:.1f} °C vs VL-Minimum "
            f"{vl_min_soll:.1f} °C (Δ {gap:+.1f} K) bei Außen {ext_now:.1f} °C. {basis} "
            + (
                f"Regelbasiert: Raster {raster:+d} (≈ 0,5–1 K VL-Soll pro Raster). "
                if raster != 0
                else "VL im Daten-Band (± 3 Raster um das VL-Minimum) — keine Empfehlung. "
            )
            + f"Ziel-VL-Soll bei Außen {ext_now:.1f} °C = {ziel:.1f} °C. "
            + "Ölbrenner/Vorlaufsoll selbst werden NIE direkt empfohlen."
        )
        return {
            "parameter": "Heizkurve/VL-Soll",
            "zone": None,
            "aktuell": (
                f"Ist-VL {hz_vl_now:.1f} °C bei Außen {ext_now:.1f} °C "
                f"(Daten-Minimum {vl_min_soll:.1f} °C → {abweich_txt})"
            ),
            "vorgeschlagen": vorgeschlagen,
            "wirkung_prognose": wirkung,
            "begruendung_de": detail,
            "detail": detail,
            "konfidenz": 0.5,
            **_exec("vl_curve", None, discovery),
            "tier": "B",
        }

    # --- Kaskade-Stufe 3: v0.5-Fallback (Standardkurve, unverändert) -----
    dev_k, band = vl_cal.curve_deviation_at(vl_calib, ext_now, hz_vl_now)
    if band is not None:
        n = int(band.get("n", 0))
        basis = (
            f"VL-Kalibrierung (Heizstunden-Only, n={n}, Außen-T-Band "
            f"{band.get('band_lo'):.0f}…{band.get('band_hi'):.0f} °C): VL-Ø "
            f"{band.get('vl_mean'):.1f} °C vs Standard-Heizkurve "
            f"{band.get('curve_vl_soll'):.1f} °C bei Außen-Ø {band.get('ext_mean'):.1f} °C "
            f"(Kalibrierung: Cache-Artefakt, Rebuild `calibrate-vl`)"
        )
    else:
        basis = (
            "Kalibrierung nicht verfügbar (Cache-Artefakt fehlt oder Außen-T-Band "
            "nicht ausreichend kalibriert, min. n = "
            f"{config.VL_KALIB_MIN_N}) → v0.4-Heuristik: Ist-VL "
            f"{hz_vl_now:.1f} °C vs Standard-Heizkurve {curve_now:.1f} °C"
        )
    # Raster-Entscheidung:
    # - **Kalibrierung vorhanden** (Band usable): Zielbild „VL so niedrig wie
    #   möglich“ (maintainer) → Absenken NUR bei data-belegtem wärmerem Betrieb
    #   (dev > +VL_RASTER_K → −1 Raster); kälterer Betrieb (dev < −
    #   VL_RASTER_K) → **kein Anheben** (Komfort via RAS-Offsets; kein
    #   data-belegtes weiteres Absenken → „beibehalten“).
    # - **Fallback** (kein usables Band, DoD-C): altes v0.4-Verhalten
    #   (Kurve-Absatz-Heuristik, beidseitig): |dev| ≥ VL_ERR_HIGH_K →
    #   „Absatz sign(dev) 1 Raster“ (VL unter/über Kurve), sonst beibehalten.
    if band is not None:
        raster = vl_cal.raster_sign(dev_k)
        if raster > 0:
            raster = 0  # kältere Abweichung → kein Anheben (Zielbild, s. oben)
    else:
        raster = (
            -1 if dev_k <= -config.VL_ERR_HIGH_K
            else 1 if dev_k >= config.VL_ERR_HIGH_K
            else 0
        )
    ziel = curve_now + raster * config.VL_RASTER_K
    # --- v0.5-engine-rework: Klartext in Hauptzeilen (maintainer: „verstehe den
    # Text nicht“); Fachjargon (Raster/Kalibrierung/n/Heizstunden-Only) →
    # Detail-Feld (UI: klein/muted, einklappbar). Die Raster-/Kalibrierungs-
    # Berechnung (Logik ``vl_calibration``) bleibt unverändert.
    if raster < 0:
        vorgeschlagen = (
            "Heizkurven-Absatz −1 Raster: 1 Raster absenken → ~0,5–1 K weniger "
            f"VL-Soll (etwas weniger Öl), ohne Komfortverlust. Ziel-VL ~{ziel:.1f} °C. "
            "Manuell im UVR/TA-Designer."
        )
        wirkung = f"VL-Soll sinkt um ~0,5–1 K (Ziel-VL ~{ziel:.1f} °C bei Außen {ext_now:.1f} °C)"
    elif raster > 0:
        vorgeschlagen = (
            "Heizkurven-Absatz +1 Raster: 1 Raster anheben → ~0,5–1 K mehr "
            f"VL-Soll (mehr Wärme). Ziel-VL ~{ziel:.1f} °C. "
            "Manuell im UVR/TA-Designer."
        )
        wirkung = f"VL-Soll steigt um ~0,5–1 K (Ziel-VL ~{ziel:.1f} °C bei Außen {ext_now:.1f} °C)"
    else:
        vorgeschlagen = "Heizkurve beibehalten (Abweichung im Raster)"
        wirkung = "keine Änderung (VL-Soll im Raster)"
    if dev_k > 0.05:
        abweich_txt = f"VL ~{dev_k:.1f} K zu hoch"
    elif dev_k < -0.05:
        abweich_txt = f"VL ~{abs(dev_k):.1f} K zu niedrig"
    else:
        abweich_txt = "VL im Raster (≈ Standard-Kurve)"
    detail = (
        f"Raster-/Kalibrierung: Abweichung {dev_k:+.1f} K bei Außen "
        f"{ext_now:.1f} °C. {basis} "
        + (
            f"Regelbasiert: Raster {raster:+d} (≈ 0,5–1 K VL-Soll pro Raster). "
            if raster != 0
            else (
                ("Abweichung im Raster (≈ 0,5–1 K) — keine Empfehlung. "
                 if abs(dev_k) <= config.VL_RASTER_K
                 else f"Innerhalb der v0.4-Schwelle (± {config.VL_ERR_HIGH_K:.0f} K) — "
                      "keine Empfehlung. ")
                if band is None
                else "Kältere Abweichung (kein Anheben empfohlen — Zielbild VL-Minimierung, "
                     "Komfort via RAS-Offsets; kein data-belegtes weiteres Absenken) — "
                     "keine Empfehlung. "
            )
        )
        + f"Ziel-VL-Soll bei Außen {ext_now:.1f} °C = {ziel:.1f} °C. "
        + "Ölbrenner/Vorlaufsoll selbst werden NIE direkt empfohlen."
    )
    return {
        "parameter": "Heizkurve/VL-Soll",
        "zone": None,
        "aktuell": (
            f"Ist-VL {hz_vl_now:.1f} °C bei Außen {ext_now:.1f} °C "
            f"(Standard-Kurve {curve_now:.1f} °C → {abweich_txt})"
        ),
        "vorgeschlagen": vorgeschlagen,
        "wirkung_prognose": wirkung,
        "begruendung_de": detail,
        "detail": detail,
        "konfidenz": 0.5,
        **_exec("vl_curve", None, discovery),
        "tier": "B",
    }


# --- v0.5 Ticket A: Minderer-Empfehlung (Advisory, je aktivem Kreis) --------


def minderer_v_target_v(
    delta_t_c: float,
    current_v: float,
    target_c: float | None = None,
    ext_c: float | None = None,
    curve: dict[str, Any] | None = None,
) -> float:
    """V-Ziel (V, 0…10) aus der ΔT-Fehlmenge (Ticket A).

    **v0.6-A (datenbasiert):** mit ``curve`` (usables Zone-Entry aus dem
    Minderer-Kurven-Cache, ``usable=True`` + ``a/b/c``) **und** ``ext_c``
    (IST-Außen-T) → Inverse der 2-Faktor-V→ΔT-Kennlinie
    ``V = a / (ΔT_ziel − b·ext − c)`` (hyperbolisch, last-normalisiert;
    Guards: Nenner ≤ 0,5 K → ``V_MAX``, ``clip(V, 0, 10)`` + Klemme
    ``min(V, v_now)``). Delegiert an
    :func:`analysis.model.l2.minderer_curve.v_from_dt_target` (Single Source
    of Truth).

    **Fallback (v0.5, exakt linear-altes Verhalten):** ``curve=None``
    (kein Zone-Fit / < 2 Punkte / Cache fehlt) **oder** ``ext_c=None``
    (keine Außen-T) → ``V_Ziel = V_aktuell · (1 − (Ziel − ΔT)/Ziel)``,
    gekappt bei ``0 V`` („max. zu“); ``ΔT ≥ Ziel`` → ``V_max`` (10 V).
    Monoton: größere ΔT-Fehlmenge → kleineres V-Ziel. ``current_v`` =
    **Ist-Vorgabe** = Sollspannung a17/18/19 (statischer Fixwert,
    UVR-extern setzbar; v0.5-engine-rework, 2026-11-12).

    Aufruf ohne ``ext``/``curve`` = exakt das alte (lineare) Verhalten
    (Signatur-Rückwärtskompatibilität, Regressionstest).
    """
    return minderer_cv.v_from_dt_target(
        delta_t_c, current_v, target_c=target_c, ext_c=ext_c, curve=curve
    )


def _minderer_advisories(
    state: State,
    rm_diag: dict[str, Any] | None,
    discovery: Any,
    minderer_curve: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ticket A-Block: Minderer-Feinjustierung je aktivem Heizkreis.

    **v0.5-engine-rework (2026-11-12, maintainer):** „Ist-Vorgabe“ =
    **Sollspannung a17/18/19** (statischer Fixwert, UVR-extern setzbar,
    30-min-Median aus ``rm_diagnostics.per_zone.minderer_soll_v``, Default
    10,0 V) — NICHT mehr ``offen-Ø × 10 V``. Das alte Gate „V-Ziele nur bei
    ≥ 2 aktiven Kreisen“ ist **entfernt**: die Druckregelpumpe O8 regelt den
    Durchfluss, auch NUR 1 Minderer kann ihn regeln. Aktiv = Minderer
    aktuell auf (a10/11/12 > 0 über das 2-h-Fenster, offen-Ø ≥
    ``RM_ACTIVE_QUOTA``; das Kriterium bleibt die Konditions-Flag-Aktiv-
    Liste, ``rm_condition()`` unangetastet). **Konditions-Flag inaktiv** →
    Block stumm. ΔT je Kreis aus der Ticket-D-Diagnose (lag-korrigiert):
    ΔT < Ziel − Toleranz → „Sollspannung runter (drosseln)“,
    ΔT > Ziel + Toleranz → „in Richtung 10 V öffnen“, dazwischen →
    „beibehalten“ — pro Strang (EG/DG/Keller) separat.

    **v0.6-A (datenbasiert, 2026-11-18):** ``minderer_curve`` (Zone-Kurven-
    Cache-Artefakt, :func:`load_minderer_curve`) + **IST-Außen-T** (aus
    ``state``, dieselbe Quelle wie die anderen Tier-B-Blöcke:
    ``_f(state.rows.get("ext"))``) → V-Ziel aus der **V→ΔT-Kennlinie**
    (2-Faktor, last-normalisiert; Inverse + Guards in
    :func:`analysis.model.l2.minderer_curve.v_from_dt_target`). Advisory-
    Text: datenbasiert → „datenbasiert (n Punkte, last-normalisiert)“;
    OoR-Außen-T-Flag (außerhalb des Fit-Kalibrierungsbands) → zusätzlicher
    Hinweis (kein Stopp); Zone ohne usables Fit (Keller/DG: < 2 Phasen-
    Punkte) → **Fallback linear exakt altes Verhalten** (Text „linear
    geschätzt“ unverändert). ``executability = advisory-uvr``
    (UVR-extern/UVR-Programm-Änderung, TA-Designer; Q-108-Analog).
    """
    out: list[dict[str, Any]] = []
    if rm_diag is None or not rm_diag.get("active"):
        return out  # Kondition inaktiv (oder keine Diagnose) → Block stumm
    cond = rm_diag.get("condition") or {}
    rm_open_2h = cond.get("rm_open_2h") or {}
    active = [z for z in config.ZONES if float(rm_open_2h.get(z, 0.0)) >= config.RM_ACTIVE_QUOTA]
    if not active:
        return out
    ext_now = _f(state.rows.get("ext"))
    per_zone = rm_diag.get("per_zone") or {}
    for z in active:
        off2 = float(rm_open_2h.get(z, 0.0))
        # Ist-Vorgabe = Sollspannung a17/18/19 (gemessener Fixwert,
        # 30-min-Median); fehlt im (alt-)Payload → Default 10,0 V.
        soll_v = (per_zone.get(z) or {}).get("minderer_soll_v")
        try:
            v_now = float(soll_v)
        except (TypeError, ValueError):
            v_now = config.RM_MINDERER_SOLL_DEFAULT_V
        v_now = float(np.clip(v_now, 0.0, config.RM_V_MAX))
        auf_txt = "Minderer aktuell auf" if off2 > 0.0 else "Minderer im 2-h-Fenster zu"
        dt = (per_zone.get(z) or {}).get("delta_t_c")
        tol = config.RM_DT_OK_TOL_K
        # v0.6-A: usables Zone-Fit (Cache-Read; None → linear-Fallback).
        z_fit = minderer_cv.zone_entry(minderer_curve, z)
        basis_txt = "linear geschätzt"
        oor_txt = ""
        if z_fit is not None and ext_now is not None:
            oob = minderer_cv.ext_oob_flag(minderer_curve, z, ext_now)
            n_fit = int(z_fit.get("n_points") or 0)
            basis_txt = f"datenbasiert ({n_fit} Punkte, last-normalisiert)"
            if z_fit.get("out_of_throttle_regime"):
                basis_txt += "; 10-V-Punkt außerhalb des Drossel-Regimes (Sättigung, Kontext)"
            if oob:
                oor_txt = (
                    f" (Außen-T {ext_now:.1f} °C außerhalb des "
                    f"Kalibrierungsbands {z_fit.get('ext_min')}…{z_fit.get('ext_max')} °C — "
                    "Extrapolation, mit Vorsicht)"
                )
        else:
            z_fit = None
        if z_fit is not None:
            basis_detail = (
                f"V-Ziel aus der datenbasierten V→ΔT-Kennlinie ({basis_txt}), "
                "Inverse V = a/(ΔT_ziel − b·Außen − c), Guards: Nenner ≤ 0,5 K → "
                "10 V, Klemme 0–10 V + min(V, v_now) (nie über die aktuelle "
                "Vorgabe hinaus öffnen)."
            )
        else:
            basis_detail = (
                "Linearer Ansatz (V-Ziel = Sollspannung × ΔT-Quotient, ohne "
                "Datenbasis); V→ΔT-Kennlinie pro Strang (EG/DG/Keller) wird ab 07.11 "
                "aus Daten gelernt (v0.6)."
            )
        if dt is not None:
            v_tz = minderer_v_target_v(dt, v_now, ext_c=ext_now, curve=z_fit)
            if dt < config.RM_DELTA_T_TARGET_C - tol:
                vorgeschlagen = (
                    f"Sollspannung runter (drosseln): {v_now:.1f} V → ~{v_tz:.1f} V "
                    f"({basis_txt})"
                )
                akt_txt = (
                    f"empfohlene Vorgabe ~{v_tz:.1f} V ({basis_txt} aus ΔT-Fehlmenge "
                    f"{config.RM_DELTA_T_TARGET_C - dt:.1f} K; Klemme 0 V = max. zu)"
                )
                dt_kurz = "Fluss zu schnell → drosseln"
            elif dt > config.RM_DELTA_T_TARGET_C + tol:
                vorgeschlagen = f"Sollspannung in Richtung {config.RM_V_MAX:.0f} V öffnen"
                akt_txt = (
                    f"empfohlene Vorgabe {config.RM_V_MAX:.0f} V (voll offen; {basis_txt})"
                )
                dt_kurz = "Fluss langsam → öffnen"
            else:
                vorgeschlagen = (
                    f"Sollspannung beibehalten (~{v_now:.1f} V, ΔT im Ziel ± {tol:.1f} K)"
                )
                akt_txt = "keine Vorgabe-Änderung (ΔT im Ziel)"
                dt_kurz = "im Ziel"
            begruendung = (
                f"Kreis {z} aktiv (Minderer aktuell auf: offen-2-h-Ø {off2:.2f} ≥ "
                f"{config.RM_ACTIVE_QUOTA}), ΔT lag-korrigiert {dt:.1f} K vs Ziel "
                f"{config.RM_DELTA_T_TARGET_C:.0f} K ({dt_kurz}). {akt_txt}. "
                + basis_detail
                + oor_txt
                + " Implementierung: Sollspannung (Fixwert) je "
                "Minderer ist von außen setzbar (UVR-Programm/externer Regler) — "
                "TA-Designer/UVR-extern."
            )
        else:
            vorgeschlagen = f"Sollspannung beibehalten (ΔT unbekannt, ~{v_now:.1f} V)"
            akt_txt = "keine Vorgabe-Änderung (ΔT nicht ermittelt)"
            begruendung = (
                f"Kreis {z} aktiv (Minderer aktuell auf: offen-2-h-Ø {off2:.2f} ≥ "
                f"{config.RM_ACTIVE_QUOTA}), ΔT nicht ermittelt (Ziel {config.RM_DELTA_T_TARGET_C:.0f} K). "
                + basis_detail
                + oor_txt
                + " Implementierung: Sollspannung (Fixwert) je Minderer von außen setzbar "
                "(UVR-Programm/externer Regler) — TA-Designer/UVR-extern."
            )
        out.append(
            {
                "parameter": f"Minderer-Feinjustierung {z}",
                "zone": z,
                "aktuell": (
                    f"Sollspannung {v_now:.1f} V (statische Vorgabe; {auf_txt}, "
                    f"offen-2-h-Ø {off2:.2f})"
                ),
                "vorgeschlagen": vorgeschlagen,
                "wirkung_prognose": akt_txt,
                "begruendung_de": begruendung,
                "konfidenz": 0.3,
                **_exec(f"minderer_{z}", z, discovery),
                "tier": "B",
            }
        )
    return out


# --- v0.5 Ticket B: Pumpen-Empfehlung O8 (Advisory, Konditions-Gating) ------


def _pump_o8_advisories(
    rm_diag: dict[str, Any] | None,
    discovery: Any,
) -> list[dict[str, Any]]:
    """Ticket B-Block: O8-Statuszeile (v0.5-engine-rework, 2026-11-12).

    **Status statt Empfehlung** (maintainer: „Pumpe ist an/aus nach Anforderung,
    Stufe manuell, nicht dynamisch“): die Druckregelpumpe O8 schaltet
    selbstständig an/aus nach Anforderung (Modus AUTO), die Stufung/der
    Druck-Soll ist manuell am physikalischen Aggregat einzustellen — es gibt
    nichts Dynamisches, was L2 empfehlen müsste. ``executability =
    advisory-manuell``, **kein** ``to_value`` (UI zeigt eine Statuszeile,
    kein Apply). **Gating:** nur bei Konditions-Flag aktiv (≥ 1 Kreis braucht
    Wärme); inaktiv → Block stumm. ``o8`` = {adresse, optionen,
    aktueller_modus} (ohne ``to_value``); ``optionen`` dient nur der Anzeige
    (Auswahl 1/2/3 = AUS/EIN/AUTO im CMI-Panel).
    """
    if rm_diag is None or not rm_diag.get("active"):
        return []
    o8_cur = _o8_exec_fields(discovery)
    # Ist-Modus aus dem Discovery (INVENTAR current_value → Option-Name):
    if o8_cur is not None:
        akt_txt = f"Modus {o8_cur} (an/aus nach Anforderung)"
    else:
        akt_txt = "Modus unbekannt (kein Discovery-Stand; an/aus nach Anforderung)"
    return [
        {
            "parameter": "Pumpe Heizkreis O8 (Modus)",
            "zone": None,
            "aktuell": akt_txt,
            "vorgeschlagen": "AUTO beibehalten — kein Wechsel nötig (Stufe/Druck-Soll manuell am Aggregat)",
            "wirkung_prognose": "Kein Wechsel nötig (Statuszeile: an/aus nach Anforderung, Stufe manuell)",
            "begruendung_de": (
                f"Druckregelpumpe O8 ({config.O8_ADDRESS}): Modus AUTO — Pumpe schaltet "
                "selbstständig an/aus nach Anforderung, Stufung/Druck-Soll ist manuell "
                "am physikalischen Aggregat (nicht dynamisch, kein CMI-Write empfohlen). "
                "Kondition aktiv (≥ 1 Kreis braucht Wärme) → O8 im Heizbetrieb. "
                "Ölbrenner/Pumpe-AUS-Anforderung selbst wird NIE empfohlen."
            ),
            "konfidenz": 0.5,
            "o8": {
                "adresse": config.O8_ADDRESS,
                "optionen": {val: name for val, name in config.O8_MODE_OPTIONS},
                "aktueller_modus": o8_cur,
            },
            "executability": config.EXECUTABILITY_ADVISORY_MANUELL,
            "cmi_hint": (
                "Druckregelpumpe O8 (10820A10180): Modus AUTO (an/aus nach "
                "Anforderung); Stufung/Druck-Soll NUR am physikalischen Aggregat "
                "(manuell). Kein CMI-Write empfohlen."
            ),
            "tier": "B",
        }
    ]


# --- Empfehlung (Orchestrierung) ---
# Sentinel: Default = LIVE-Forecast laden (Cache/HTTP). None = bewusster
# Fallback auf Store-Wetter (stale); WxLive = injiziert (Tests).
FETCH = "fetch-live-forecast"


@dataclass
class Recommendation:
    """Stunden-Empfehlung (JSON-Serial, Design §3.2 + L2-v0.1-Felder)."""

    ts: str
    model_version: str
    horizon_h: int
    state: State
    wx: weather.WxLive | None
    stale: bool
    stale_reasons: list[str]
    empfehlungen: list[dict[str, Any]]
    kosten: dict[str, Any]  # Kosten-Abbildung (Kandidaten + Basis)
    guardrail_hits: list[str]
    konfidenz: float
    n_candidates: int
    n_before_prune: int
    candidate_list: list[dict[str, Any]]
    # v0.5 Ticket D: ΔT-Diagnose + Lag + Konditions-Flag (advisory, neu).
    # ``None`` = nicht berechnet (z. B. Stale-Pfad) → JSON-Feld = None.
    rm_diagnostics: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "model_version": self.model_version,
            "horizon_h": self.horizon_h,
            "stale": self.stale,
            "stale_reasons": self.stale_reasons,
            "empfehlungen": self.empfehlungen,
            "kosten": self.kosten,
            "guardrail_hits": self.guardrail_hits,
            "konfidenz": round(self.konfidenz, 3),
            "szenarien": [],  # v0.1: keine Szenario-Abfragen (v2)
            # v0.5 Ticket D: ΔT-Diagnose + pro-Etage-Lag + Konditions-Flag
            # („≥ 1 Kreis braucht Wärme“; inaktiv → Block stumm).
            "rm_diagnostics": self.rm_diagnostics,
            "kontext": {
                "datenstand": self.state.store_last_ts,
                "state_ts": self.state.state_ts_str,
                "metric_gap_h": round(self.state.metric_gap_h, 2),
                "data_version": self.state.data_version,
                "wx_quelle": self.wx.wx_quelle if self.wx else "Store-Wetter (Fallback, kein Live-Forecast)",
                "wx_run": self.wx.wx_run if self.wx else None,
                "wx_age_h": None if self.wx is None or self.wx.wx_age_h is None else round(self.wx.wx_age_h, 2),
                "wx_fallback": self.wx is None,
                "stale": self.stale,
            },
        }


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _reason_no_change(detail: str) -> str:
    return (
        "Alle Kandidaten innerhalb der No-change-Schwelle (Kosten-Delta < "
        f"{config.NO_CHANGE_MIN_DELTA}) oder per Guardrail verworfen: aktuelle Offsets beibehalten. {detail}"
    )


def _exec(
    parameter: str, zone: str | None, discovery: Any
) -> dict[str, str]:
    """Executability-Felder (v0.2, discovery-basiert) für eine Empfehlung."""
    ex, hint = executability.executability_for(parameter, zone, discovery)
    return {"executability": ex, "cmi_hint": hint}


def _o8_exec_fields(discovery: Any) -> str | None:
    """v0.5-engine-rework: O8-aktueller Modus-Name (INVENTAR-``current_value``
    → Option-Name, z. B. „AUTO“); unbekannt (kein Discovery/Eintrag) →
    ``None``. Die v0.5-B-Executability-Mix (Modus/Stufung) entfällt: die O8-
    Empfehlung ist eine **Statuszeile** (``advisory-manuell``, kein
    ``to_value``, s. :func:`_pump_o8_advisories`).
    """
    ent = executability._entry_for_parameter("pump_o8", discovery)
    if ent is not None and ent.current_value:
        return dict(config.O8_MODE_OPTIONS).get(str(ent.current_value))
    return None


def _zone_advice(
    zone: str,
    off_now: float,
    off_new: float,
    cost: CostBreakdown | None,
    base_cost: CostBreakdown,
    wx: weather.WxLive | None,
    state: State,
    conf: float,
    changed: bool,
    discovery: Any = None,
    minderer: tuple[float | None, str] = (None, "unbekannt"),
) -> dict[str, Any]:
    """Tier-A-Advice je Zone (v0.3-FIX + Effektor-Guard Fix-2, v0.5-rework
    RAS-De-Emphasis).

    ``minderer`` = (:func:`minderer_state`) je Zone: „zu“ ergänzt den
    Grundtext (Offset hat keine Stellkraft, Heizkreis aus); „flappend“ +
    Raum außerhalb des Komfort-Bands ergänzt den Heizkurve/Hinweis (Advisory
    im Text, keine Kandidaten-Untersagung).

    ``cost``/``base_cost`` bleiben im Signature (JSON-``kosten``-Block trägt
    die Optimierungs-Details); die ``begruendung_de`` ist de-emphasized
    (2026-11-12): state-Text + Einzeiler-Handlung
    „Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen (Offset intern)“,
    keine Kosten-Erklärung (die primäre RAS-Handlung ist das Raum-Soll am
    Gerät; das stündliche Offset ist Feinjustierung).
    """
    _off_3h, m_status = minderer
    ist_now = _h(state.rows.get(f"ras_{zone}_ist")) or 0.0
    soll_now = config.RAS_BASE_C - off_now
    tmin = weather.wx_min_12h(wx) if wx else None
    dev_now = ist_now - soll_now  # + = zu warm (vorzeichnungsgetreu, v0.3)
    wx_note = (
        f" Außer-T-Prognose 12 h: Minimum {tmin:+.1f} °C (ICON-D2, Run {wx.wx_run})."
        if wx and tmin is not None
        else ""
    )
    state_txt = f"Ist {ist_now:.1f} °C (Soll {soll_now:.1f} °C, Abw. {dev_now:+.1f} K)"
    # --- v0.5-engine-rework (2026-11-12, maintainer: RAS-Offset de-emphasize) ---
    # Die lange Optimierungs-/Kosten-Erklärung entfällt: die Handlung ist der
    # Einzeiler „Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen
    # (Offset intern)“. Das ``state_txt``-Präfix („Ist … (Soll …, Abw. …)“)
    # und die Effektor-Guard-Appendings (Fix-2) bleiben (gepinnte
    # Regressionstexte, s. test_v003_*); Kosten-Details bleiben im JSON-
    # ``kosten``-Block (Tier A = stündliche Feinjustierung, die primäre
    # Handlung ist das Raum-Soll am Gerät).
    detail = f"{state_txt}."
    detail += (
        " Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen (Offset intern)."
    )
    # --- Effektor-Guard (Fix-2): Rücklaufminderer 3-h-Fenster ---
    if m_status == "zu":
        # Grundtext-Ende (statt/plus dem „keine kostengünstigere …“-Text):
        # für diese Zone wurden alle Δ≠0 unterdrückt (Kandidaten-Filter).
        detail += (
            " Rücklaufminderer zu seit 3 h (perzipiert > 20,1 °C) — Heizkreis aus, "
            "Offset hat keine Stellkraft (externe Wärmequelle, z. B. Trocknungsgerät)."
        )
    elif m_status == "flappend" and abs(dev_now) > COMFORT_BAND_K:
        # Advisory im Text (KEINE Kandidaten-Untersagung — der Kreis schaltet
        # noch, Offset wirkt teilweise). EG-False-Positiv-Absicherung: im
        # Band (z. B. Abw. +0,1) bleibt der Hinweis aus.
        detail += (
            " Heizkreis aktiviert periodisch (Minderer-Flap, 3-h-Fenster) — deutet auf "
            "unpassende Heizkurve (Q-108) oder Rücklaufminderer-Einstellung des Heizkreises hin."
        )
    # v0.5-engine-rework: maintainer-Beleg-Format (Klartext) je Zone als eigenes
    # Feld (UI: prominent); die Offset-Optimierung selbst bleibt de-emphasized
    # (stündliche Feinjustierung, primär = Raum-Soll am Gerät).
    raum_text = (
        f"Raum {zone}: {ist_now:.1f} °C (Ist) vs {soll_now:.1f} °C (Soll; "
        f"Abw. {dev_now:+.1f} K)"
    )
    return {
        "parameter": f"RAS-Offset {zone}",
        "zone": zone,
        "raum_text": raum_text,
        "kanal": f"dev1 (RAS {zone}): perzipiert = Ist + Offset, Offset = Stellgröße",
        "aktuell": f"{off_now:+.1f} K",
        "vorgeschlagen": f"{off_new:+.1f} K",
        "wirkung_prognose": ("Offset über 12 h gehalten (L1-Rollout)" if changed else "keine Änderung"),
        "begruendung_de": f"{detail}{wx_note}" + (
            " Advisory (Offset intern, stündliche Feinjustierung)." if changed else ""
        ),
        "konfidenz": round(conf, 3),
        **_exec(f"offset_{zone}", zone, discovery),
        "tier": "A",
        "changed": changed,
    }


def recommend(
    ts: datetime | str,
    features_dir: Path | None = None,
    artifact_dir: Path | None = None,
    runs_dir: Path | None = None,
    predictor: Any | None = None,
    wx: weather.WxLive | None | object = FETCH,
    fetcher: Any | None = None,
    skill_map: dict[tuple[str, int], float] | None = None,
) -> Recommendation:
    """Empfehlung für die volle Berliner Stunde ``ts`` (deterministisch).

    ``wx``: ``FETCH`` (Default) = LIVE-Forecast laden (Cache/HTTP, s.
    ``weather.fetch_live``); ``None`` = bewusster Fallback auf Store-Wetter
    (stale); ``WxLive`` = injiziert (Tests). ``predictor`` = L1Predictor
    (Default: v003 laden).
    """
    state = load_state(ts, features_dir=features_dir)
    requested = weather.berlin_hour(ts)
    requested_ts_str = f"{requested:%Y-%m-%d %H:00}"
    skill_map = skill_map if skill_map is not None else load_skill_map(artifact_dir)
    conf_base = base_confidence(skill_map)
    # v0.2: Discovery einmal pro recommend()-Lauf laden (fail-closed: None →
    # kuratierte Advisory-Labels, nie cmi-settable; s. docs/l2-executability.md).
    discovery = executability.load_discovery()

    # --- Wetter: LIVE-Forecast (Cache/HTTP) oder Fallback ---
    if wx is FETCH:
        bias = weather.load_bias_profile()
        wx_obj = (fetcher or weather.fetch_live)(requested, runs_dir=runs_dir, bias_profile=bias)
    else:
        wx_obj = wx if isinstance(wx, weather.WxLive) else None

    # --- Stale-Guard (hart, vor Ausgabe; Spec: Lücke > 1 h ODER Run > 2 h alt) ---
    # Lücke (ganzzahlig) > 1,5 h ≙ ≥ 2 fehlende Buckets ≙ Spec "> 1 h".
    stale_reasons: list[str] = []
    if state.metric_gap_h > config.STORE_STALE_MAX_H:
        stale_reasons.append(
            f"Metrik-Lücke {state.metric_gap_h:.1f} h: Zustand {state.state_ts_str}, Request "
            f"{requested_ts_str} (schwelle {config.STORE_STALE_MAX_H:.1f} h; letzter Bucket {state.store_last_ts})"
        )
    if state.gap_full:
        stale_reasons.append(f"Store-Zeile {state.state_ts_str} mit gap_full (vollständige Daten-Lücke)")
    if wx_obj is None:
        stale_reasons.append("LIVE-Forecast nicht erreichbar → Fallback auf Store-Wetter (wx_cache/Ist)")
    elif wx_obj.wx_age_h is None:
        stale_reasons.append("Wetter-Run-Alter unbekannt (generationtime_ms fehlt) → konservativ stale")
    elif wx_obj.wx_age_h > config.WX_RUN_STALE_MAX_H:
        stale_reasons.append(f"Wetter-Run {wx_obj.wx_age_h:.1f} h alt (> {config.WX_RUN_STALE_MAX_H:.0f} h)")
    stale = bool(stale_reasons)

    if stale:
        rec = _stale_recommendation(
            state, requested_ts_str, stale_reasons, wx_obj, conf_base, discovery
        )
        return rec

    # --- Kandidaten + Guardrails ---
    offsets = state.offsets
    cands = generate_candidates(offsets)
    cands, guardrail_hits = apply_guardrails(cands, state, wx_obj)
    # --- Effektor-Guard (Fix-2): Rücklaufminderer 3-h-Fenster ---
    # Zustand „zu“ (seit ~3 h, perzipiert > 20,1 °C): der RAS kann heizen,
    # aber NICHT kühlen → der Offset hat keine Stellkraft (Raum wird von
    # externer Wärmequelle, z. B. Trocknungsgerät, gehalten) → alle
    # Kandidaten mit Δ_z ≠ 0 entfernen (Δ_z = 0 erzwingen); No-Change bleibt
    # immer enthalten (Grid enthält (0,0,0), Guardrails werfen es nie weg).
    minderer = {z: minderer_state(state.rows, z) for z in config.ZONES}
    zu_zones = [z for z in config.ZONES if minderer[z][1] == "zu"]
    if zu_zones:
        kept = [c for c in cands if all(c[z] == 0.0 for z in zu_zones)]
        if not kept:
            kept = [{z: 0.0 for z in config.ZONES}]
        guardrail_hits.extend(
            f"minderer {z}: 3-h-Ø(open) {minderer[z][0]:.2f} ≤ {config.MINDERER_CLOSED_MAX:.1f} "
            "(zu) → Offset-Stellkraft fehlt, Δ=0 erzwungen"
            for z in zu_zones
        )
        cands = kept
    if not cands:
        cands = [{z: 0.0 for z in config.ZONES}]

    # --- Rollout + Kosten (deterministisch: sortierte Kandidaten-Reihenfolge) ---
    p = predictor
    if p is None:
        from analysis.model.m2b.predict import L1Predictor

        p = L1Predictor.load(artifact_dir)
    raw_cols = list(dict.fromkeys(list(p.raw_inputs) + list(ROLLOUT_TARGETS) + list(OFFSET_STEP_COLS)))
    from analysis.model.m2b.data import load_store

    tsa, raw_rows, _ = load_store(features_dir, columns=raw_cols, where=f"ts = TIMESTAMP '{state.state_ts_str}'")
    raw = {k: v for k, v in raw_rows.items()}
    tsa = tsa[:1]
    raw = {k: v[:1] for k, v in raw.items()}
    if wx_obj is not None:
        raw = inject_live_wx(raw, wx_obj)

    scored: list[tuple[dict[str, float], CostBreakdown]] = []
    for cand in cands:
        ro = rollout(p, state.state_ts_str, raw, tsa, cand)
        scored.append((cand, candidate_cost(ro, cand, offsets)))
    base = next((c, b) for c, b in scored if all(d == 0.0 for d in c.values()))
    scored.sort(key=lambda cb: (cb[1].total, sum(abs(d) for d in cb[0].values())))
    best_cand, best_cost = scored[0]
    base_cost = base[1]

    # --- No-change-by-default ---
    changed = (best_cost.total < base_cost.total - config.NO_CHANGE_MIN_DELTA) and any(
        d != 0.0 for d in best_cand.values()
    )

    # --- v0.5 Ticket D: ΔT-Diagnose + pro-Etage-Lag + Konditions-Flag ---
    # Rein diagnostisch/advisory (neue Datenfelder für Tickets A/B/C + UI).
    # Lag aus dem Cache-Artefakt (kein pro-Call-Neu-Scan); ΔT bevorzugt auf
    # dem 10-s-Roh-Fenster (2 h), Fallback = stündlicher Store-2-h-Ø. Inaktiv
    # (kein Kreis braucht Wärme) → per_zone stumm. Fail-safe: Rohdaten/Store
    # fehler → Defaults (kein Crash, kein CMI-Zugriff).
    try:
        lag_art = load_lag_estimate(features_dir=features_dir)
        raw_win = load_raw_window(features_dir=features_dir, ts=state.ts)
        diag = rm_diag.compute_diagnostics(
            state.rows, lag_art=lag_art, raw=raw_win
        )
        rm_diag_dict: dict[str, Any] | None = diag.to_dict()
    except Exception:  # noqa: BLE001 - diagnostisch: kein Crash, fail-safe
        rm_diag_dict = None

    # --- Tier-B-Advisories (regelnbasiert, kein Rollout) ---
    # v0.5: Tickets A/B (Minderer/Pumpe) gated durch das Ticket-D-
    # Konditions-Flag (``rm_diag_dict``; inaktiv → stumm); Ticket C
    # (Heizkurve) liest das VL-Kalibrierungs-Cache-Artefakt (kein
    # Historie-Scan; fehlt → v0.4-Heuristik + Hinweis).
    # v0.6-B: + VL-Minimum-Cache-Artefakt (Kaskade datenbasiert; fehlt →
    # Kaskade überspringt Stufe 2, v0.5-Verhalten bleibt exakt).
    # v0.6-A: + Minderer-V→ΔT-Kurven-Cache (Zone-Kennlinie; fehlt →
    # linear-Fallback, exakt v0.5-Verhalten).
    vl_calib = load_vl_calibration(features_dir=features_dir)
    vl_min_art = load_vl_minimum(features_dir=features_dir)
    minderer_curve_art = load_minderer_curve(features_dir=features_dir)
    tier_b = tier_b_advisories(
        state,
        wx_obj,
        guardrail_hits,
        discovery,
        rm_diag=rm_diag_dict,
        vl_calib=vl_calib,
        vl_min_art=vl_min_art,
        minderer_curve=minderer_curve_art,
    )

    # --- Konfidenz ---
    conf = conf_base if changed else min(config.CONF_NO_CHANGE, config.CONF_CAP)
    if stale:
        conf *= config.CONF_STALE_PENALTY

    # --- Empfehlung je Zone (Tier A) + Tier B ---
    # 0,5-K-Quantisierung gilt für die **Änderung** (ΔK), nicht den absoluten
    # Offset (der RAS-Offset im Store kann 0,1-K-Auflösung haben, z. B. −1,8 K).
    emp: list[dict[str, Any]] = []
    for z in config.ZONES:
        off_now = offsets[z]
        zone_changed = changed and best_cand[z] != 0.0
        off_new = off_now + best_cand[z] if zone_changed else off_now
        emp.append(
            _zone_advice(
                z, off_now, off_new, best_cost if zone_changed else None, base_cost,
                wx_obj, state, conf, zone_changed, discovery,
                minderer=minderer[z],
            )
        )
    for adv in tier_b:
        adv["konfidenz"] = round(min(adv["konfidenz"], config.CONF_CAP), 3)
        emp.append(adv)

    kosten = {
        "basis": {"kandidat": candidate_key(base[0]), **base[1].as_dict()},
        "beste": {"kandidat": candidate_key(best_cand), **best_cost.as_dict()},
        "kandidaten": [
            {"kandidat": candidate_key(c), **b.as_dict()} for c, b in scored
        ][:10],
        "gewichte": {"w_comfort": config.W_COMFORT, "w_energy": config.W_ENERGY, "w_change": config.W_CHANGE},
    }

    return Recommendation(
        ts=requested_ts_str,
        model_version=config.model_version(),
        horizon_h=12,
        state=state,
        wx=wx_obj,
        stale=False,
        stale_reasons=[],
        empfehlungen=emp,
        kosten=kosten,
        guardrail_hits=sorted(set(guardrail_hits)),
        konfidenz=round(conf, 3),
        n_candidates=len(cands),
        n_before_prune=len(ALL_GRID),
        candidate_list=[
            {"kandidat": candidate_key(c), "cost": b.total, "best": i == 0} for i, (c, b) in enumerate(scored)
        ],
        rm_diagnostics=rm_diag_dict,
    )


def _stale_recommendation(
    state: State,
    ts_str: str,
    stale_reasons: list[str],
    wx: weather.WxLive | None,
    conf_base: float,
    discovery: Any = None,
) -> Recommendation:
    """Stale-Pfad (Design §3.2 R5): nur „beibehalten“, keine Modell-Aktionen."""
    conf = round(conf_base * config.CONF_STALE_PENALTY, 3)
    detail = "; ".join(stale_reasons)
    emp: list[dict[str, Any]] = []
    for z in config.ZONES:
        off = _h(state.rows.get(f"offset_{z}")) or 0.0
        emp.append(
            {
                "parameter": f"RAS-Offset {z}",
                "zone": z,
                "kanal": f"dev1 (RAS {z}): perzipiert = Ist + Offset",
                "aktuell": f"{off:+.1f} K",
                "vorgeschlagen": f"{off:+.1f} K (beibehalten)",
                "wirkung_prognose": "keine Änderung",
                "begruendung_de": f"Stale-Status: {detail}. Empfehlung: beibehalten.",
                "konfidenz": conf,
                **_exec(f"offset_{z}", z, discovery),
                "tier": "A",
                "changed": False,
            }
        )
    return Recommendation(
        ts=ts_str,
        model_version=config.model_version(),
        horizon_h=12,
        state=state,
        wx=wx,
        stale=True,
        stale_reasons=stale_reasons,
        empfehlungen=emp,
        kosten={
            "basis": {},
            "beste": {},
            "kandidaten": [],
            "gewichte": {"w_comfort": config.W_COMFORT, "w_energy": config.W_ENERGY, "w_change": config.W_CHANGE},
        },
        guardrail_hits=[],
        konfidenz=conf,
        n_candidates=0,
        n_before_prune=0,
        candidate_list=[],
    )


# --- Report ---


def write_report(rec: Recommendation, json_path: Path, out_path: Path | None = None) -> Path:
    """Human-readable DE-Report (REPORT.md): Top-3 + Advisory + Guardrails + Stale + Konfidenz."""
    out = Path(out_path) if out_path else config.REPORT_PATH
    j = rec.to_json()
    lines: list[str] = []
    lines.append("# L2 v0.1 — Empfehlung (Advisory, offline)")
    lines.append("")
    lines.append(f"- **Zeitpunkt:** {rec.ts} (Europe/Berlin)")
    lines.append(f"- **model_version:** `{rec.model_version}` · Horizont {rec.horizon_h} h (6-h-Zwischencheck)")
    lines.append(f"- **stale:** `{rec.stale}`" + (f" — {'; '.join(rec.stale_reasons)}" if rec.stale_reasons else ""))
    k = j["kontext"]
    lines.append(
        f"- **Datenstand:** {k['datenstand']} (data_version {k['data_version']}, Zustand {k['state_ts']}, "
        f"Metrik-Lücke {k['metric_gap_h']} h)"
    )
    lines.append(
        f"- **Wetter:** {k['wx_quelle']}"
        + (f", Run {k['wx_run']}" if k.get("wx_run") else "")
        + f", Alter {k.get('wx_age_h')} h"
        + (" (FALLBACK: Store-Wetter)" if k.get("wx_fallback") else "")
    )
    lines.append(f"- **Konfidenz (Basis):** {rec.konfidenz}")
    lines.append("")
    lines.append("## Top-3-Empfehlungen je Zone (Tier A)")
    lines.append("")
    tier_a = [e for e in j["empfehlungen"] if e.get("tier") == "A"]
    for e in tier_a[:3]:
        mark = "➜ ändern" if e.get("changed") else "● beibehalten"
        lines.append(f"### {e['parameter']} — {mark}")
        lines.append("")
        lines.append(f"- aktuell: **{e['aktuell']}** → vorgeschlagen: **{e['vorgeschlagen']}**")
        lines.append(f"- Konfidenz: {e['konfidenz']} · Executability: `{e['executability']}` ({e.get('cmi_hint')})")
        lines.append(f"- Begründung: {e['begruendung_de']}")
        lines.append("")
    lines.append("## Tages-Advisory (Tier B, regelnbasiert)")
    lines.append("")
    tier_b = [e for e in j["empfehlungen"] if e.get("tier") == "B"]
    if tier_b:
        for e in tier_b:
            lines.append(f"- **{e['parameter']}:** {e['vorgeschlagen']} (Konfidenz {e['konfidenz']}, `{e.get('cmi_hint')}`)")
        lines.append("")
    else:
        lines.append("- (kein Advisory ausgelöst — Schwellen nicht erreicht)")
        lines.append("")
    # --- v0.5 Ticket D: ΔT-Diagnose + pro-Etage-Lag + Konditions-Flag ---
    lines.append("## ΔT-Diagnose & Kondition (v0.5 Ticket D, advisory)")
    lines.append("")
    rm_diag = j.get("rm_diagnostics")
    if rm_diag is None:
        lines.append("- (nicht berechnet: Stale-Pfad / keine Rohdaten — Block stumm)")
        lines.append("")
    else:
        cond = rm_diag.get("condition") or {}
        if rm_diag.get("active"):
            lines.append(
                f"- **Kondition aktiv** („≥ 1 Kreis braucht Wärme“): "
                f"Pumpe-3-h-Ø {cond.get('pump_3h_avg'):.2f}, "
                + ", ".join(
                    f"{z} offen {v:.2f}"
                    for z, v in (cond.get("rm_open_2h") or {}).items()
                )
                + f" → Zonen: {', '.join(cond.get('active_zones') or [])}."
            )
        else:
            lines.append(
                "- **Kondition inaktiv** (kein Kreis braucht Wärme) → "
                "ΔT/Lag-Block stumm („VL niedrig“ ist sonst nicht definiert)."
            )
        for z in config.ZONES:
            zz = (rm_diag.get("per_zone") or {}).get(z)
            if zz is None:
                continue
            dt = zz.get("delta_t_c")
            dt_s = f"{dt:.2f} K" if dt is not None else "—"
            gap = zz.get("delta_t_gap_c")
            gap_s = f"{gap:+.2f} K" if gap is not None else "—"
            lines.append(
                f"- **{z}**: ΔT {dt_s} (Ziel {zz.get('delta_t_target_c'):.0f} K, "
                f"Gap {gap_s}) · Lag {zz.get('lag_s')} s "
                f"({zz.get('lag_source')}, Konf {zz.get('lag_confidence')}) · "
                f"Fluss {zz.get('flow_class')} (offen-2-h-Ø {zz.get('rm_open_2h'):.2f})"
            )
        lines.append("")
    lines.append("## Guardrail-Hits (hart, vor Ausgabe)")
    lines.append("")
    if rec.guardrail_hits:
        for h in rec.guardrail_hits:
            lines.append(f"- {h}")
    else:
        lines.append("- (keine Guardrail-Verletzung)")
    lines.append("")
    lines.append("## Kosten-Abbildung (Top-Kandidaten)")
    lines.append("")
    ko = j.get("kosten") or {}
    if ko.get("kandidaten"):
        lines.append("| Kandidat | Komfort | Energie | Change | Total |")
        lines.append("|---|---|---|---|---|")
        for row in ko["kandidaten"]:
            c = row["cost"] if "cost" in row else row.get("total")
            lines.append(
                f"| {row['kandidat']} | {row.get('comfort', '—')} | {row.get('energy', '—')} | {row.get('change', '—')} | {row.get('total', c)} |"
            )
        lines.append("")
        lines.append(f"- Kandidaten: {rec.n_candidates} nach Pruning (von {rec.n_before_prune})")
    else:
        lines.append("- (stale: keine Rollouts gerechnet)")
    lines.append("")
    lines.append("## Executability-Map (v0.2: discovery-basiert, je-Empfehlung)")
    lines.append("")
    for e in j["empfehlungen"]:
        lines.append(f"- `{e['parameter']}`: `{e['executability']}` — {e.get('cmi_hint')}")
    lines.append("")
    lines.append(
        f"- Fail-Closed: INVENTAR.json fehlt/ungültig → kuratierte Advisory-Labels "
        f"(Fallback `{config.EXECUTABILITY_PENDING}` nur bei unbekannten Parametern; "
        f"nie `{config.EXECUTABILITY_CMI_SETTABLE}`, RISK-Schalter immer `{config.EXECUTABILITY_RISK_BLOCKED}`). "
        "Kein `adresse`/`to_value` — UI-„Anwenden“ bleibt inaktiv (s. docs/l2-executability.md)."
    )
    lines.append("## Quellen")
    lines.append("")
    lines.append(f"- JSON: `{json_path}`")
    lines.append(f"- L1: `{config.ARTIFACT_DIR}` (cmi-brain-v003, READ-ONLY)")
    lines.append(f"- Feature-Store: `{config.FEATURES_DIR}` (m1.3, READ-ONLY)")
    lines.append("- Wetter: Open-Meteo ICON-D2 LIVE (non-commercial) + M1-Bias-Profil (dev0 a8)")
    lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def save_json(rec: Recommendation, ts_str: str, runs_dir: Path | None = None) -> Path:
    """JSON nach ``runs/recommend_<ts>.json`` (ts = 'YYYY-MM-DD_HH00')."""
    p = Path(runs_dir or config.RUNS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    tag = ts_str.replace(" ", "_").replace(":", "")
    out = p / f"recommend_{tag}.json"
    out.write_text(json.dumps(rec.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    return out
