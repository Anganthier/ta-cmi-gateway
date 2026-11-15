"""M2b — Offset-Responsivitäts-Test v2 (P3, Design §3.1, 18.10-Muster).

Korrekturen gegenüber M2 (``m2.responsivity``):

1. **Konsistenter Eingriff:** Offset **und** perzipierte Temperatur der Zone werden
   gemeinsam um Δ verschoben (perzipiert = Ist + Offset, Q-100); M2 hob nur
   den Offset an → widersprüchlicher Zustand.
2. **Wirkungsgröße = Stellglied der Etage** (Q-110, maintainer 2026-11-01): Jede Etage hat
   einen eigenen Rücklaufminderer (UVR610, ``hzrkl_{z}_on`` / ``hzrkl_{z}_pos``), die
   Pumpe d8 ist gemeinsam (RT = MIN). Ein Etagen-Offset wirkt daher primär über den
   Minderer der Etage; die Pumpe reagiert nur, wenn die Etage die kälteste ist.
   DoD-Größe = erste trainierte aus :func:`config.resp_effects` (Ventil vor Pumpe).
   Zusätzlich (informativ): Pumpe, Ventilstellung, RAS-Ist-Delta der Zone.
3. **Heizsaison-Fälle:** nur Zustände mit Außen-T < 12 °C.
4. **Direkte Modelle:** Wirkung = mittlere Differenz der Vorhersagen über
   h ∈ ``RESP_HORIZONS`` (Step vs. Hold); kein Rollout nötig.
5. **DoD-Zone EG** (größte Heizlast); Bericht für alle Zonen (``RESP_ZONES``).

DoD (hart): ``Pearson(Δ, ΔWirkung) ≤ −0,6`` und bei Δ = +3,4 K ``ΔWirkung < 0`` in ≥ 80 %.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import config
from .data import Dataset
from .features import apply_offset_step, derive

PredFn = Callable[[str, int, np.ndarray], np.ndarray]


@dataclass
class RespResult:
    zone: str
    n_cases: int
    deltas: tuple[float, ...]
    primary: str
    corr: dict[str, float]
    frac_neg: dict[str, float]
    per_delta: dict[str, dict[str, float]] = field(default_factory=dict)
    passed: bool = False

    @property
    def corr_primary(self) -> float:
        return self.corr.get(self.primary, float("nan"))

    def as_dict(self) -> dict:
        k = config.RESP_TARGET_DELTA
        return {
            "zone": self.zone,
            "n_cases": self.n_cases,
            "deltas_k": list(self.deltas),
            "primary": self.primary,
            "pearson_primary": _r(self.corr_primary),
            f"frac_neg_primary_at_{k}K": _r(self.frac_neg.get(self.primary, float("nan"))),
            "pearson": {e: _r(v) for e, v in self.corr.items()},
            f"frac_neg_at_{k}K": {e: _r(v) for e, v in self.frac_neg.items()},
            "per_delta": self.per_delta,
            "dod_passed": self.passed,
        }


def _r(v: float) -> float | None:
    return None if v is None or not np.isfinite(v) else round(float(v), 4)


def select_cases(
    ds: Dataset,
    split: str = "valid",
    n_cases: int = config.RESP_N_CASES,
    seed: int = config.SEED,
    zone: str = config.RESP_ZONE,
) -> np.ndarray:
    """Zufällige Heizsaison-Zustände mit gültigen RAS-/Offset-Werten der Zone (Seed fest)."""
    raw = ds.raw
    with np.errstate(invalid="ignore"):
        ok = (
            ds.rows(split)
            & (raw["gap_full"] == 0)
            & np.isfinite(raw[f"offset_{zone}"])
            & np.isfinite(raw[f"ras_{zone}_perc"])
            & np.isfinite(raw[f"ras_{zone}_ist"])
            & (raw[f"ras_age_{zone}"] <= config.RAS_STALE_AGE)
            & (raw["ext"] < config.RESP_HEATING_EXT_MAX)
        )
    idx = np.flatnonzero(ok)
    if len(idx) == 0:
        raise ValueError(f"keine geeigneten Responsivitäts-Zustände (Zone {zone}, Split {split})")
    rng = np.random.default_rng(seed)
    pick = rng.choice(idx, size=min(n_cases, len(idx)), replace=False)
    return np.sort(pick)


def _subset(raw: dict[str, np.ndarray], rows: np.ndarray) -> dict[str, np.ndarray]:
    return {k: np.asarray(v)[rows] for k, v in raw.items()}


def run(
    ds: Dataset,
    pred_fn: PredFn,
    cases: np.ndarray | None = None,
    deltas: tuple[float, ...] = config.RESP_DELTAS_K,
    zone: str = config.RESP_ZONE,
    horizons: tuple[int, ...] = config.RESP_HORIZONS,
    available: set[str] | None = None,
) -> RespResult:
    """Führt den Test für ``zone`` aus. ``pred_fn(target, h, X)`` = Modell-Ausgabe.

    ``available`` = trainierte Targets (Default: alle aus ``resp_effects`` + RAS-Ist);
    nicht trainierte Wirkungsgrößen werden übersprungen.
    """
    if cases is None:
        cases = select_cases(ds, zone=zone)
    effects = [e for e in config.resp_effects(zone) if available is None or e in available]
    if not effects:
        raise ValueError(f"keine Wirkungsgröße für Zone {zone} trainiert")
    t_ist = f"ras_{zone}_ist"
    with_ist = available is None or t_ist in available
    raw_c = _subset(ds.raw, cases)
    ts_c = ds.ts[cases]
    X_hold, _ = derive(raw_c, ts_c)
    h_t = max(horizons)

    def effect(X: np.ndarray) -> dict[str, np.ndarray]:
        out = {e: np.mean([pred_fn(e, h, X) for h in horizons], axis=0) for e in effects}
        if with_ist:
            out["ras_ist"] = pred_fn(t_ist, h_t, X)
        return out

    hold = effect(X_hold)
    names = list(hold.keys())
    all_d: list[float] = []
    diffs: dict[str, list[float]] = {e: [] for e in names}
    per: dict[str, dict[str, float]] = {}
    frac_at: dict[str, float] = {}
    for delta in deltas:
        X_step, _ = derive(apply_offset_step(raw_c, zone, delta), ts_c)
        step = effect(X_step)
        all_d.extend([delta] * len(cases))
        row: dict[str, float] = {}
        for e in names:
            d = step[e] - hold[e]
            diffs[e].extend(d.tolist())
            row[f"mean_d_{e}"] = round(float(np.mean(d)), 5)
            row[f"frac_neg_{e}"] = round(float(np.mean(d < 0)), 4)
            if abs(delta - config.RESP_TARGET_DELTA) < 1e-9:
                frac_at[e] = float(np.mean(d < 0))
        per[f"+{delta:.1f}K"] = row
    d_arr = np.asarray(all_d)
    corr = {e: _corr(d_arr, np.asarray(diffs[e])) for e in names}
    primary = effects[0]
    c, f = corr[primary], frac_at.get(primary, float("nan"))
    passed = bool(np.isfinite(c) and c <= config.RESP_CORR_MAX and f >= config.RESP_FRAC_MIN)
    return RespResult(zone, len(cases), tuple(deltas), primary, corr, frac_at, per, passed)


def run_all_zones(
    ds: Dataset,
    pred_fn: PredFn,
    available: set[str],
    split: str = "valid",
    n_cases: int = config.RESP_N_CASES,
    zones: tuple[str, ...] = config.RESP_ZONES,
) -> dict[str, dict]:
    """Test je Zone; Ergebnis ``{zone: as_dict()}`` + ``'dod_zone'``/``'dod_passed'``."""
    out: dict[str, dict] = {}
    for z in zones:
        cases = select_cases(ds, split=split, n_cases=n_cases, zone=z)
        out[z] = run(ds, pred_fn, cases=cases, zone=z, available=available).as_dict()
    dz = config.RESP_ZONE if config.RESP_ZONE in out else zones[0]
    out["dod_zone"] = dz
    out["split"] = split
    out["dod_passed"] = bool(out[dz]["dod_passed"])
    return out


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(set(a.tolist())) < 2 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])
