"""M2b — Metriken, Baselines, DoD-Auswertung.

Baselines (je Target × Horizont, auf **denselben** gültigen Zeilen wie das
Modell):

- **B0 Persistence (Pflicht-DoD):** ``ŷ(t+h) = y(t)`` (Stundenmittel).
- **B0-last:** ``ŷ(t+h) = y_last(t)`` (letzter 10-s-Wert der Stunde; nur wo
  ``{target}_last`` existiert) — strengere, informative Persistence.
- **B2 Naive (Design §5, informativ):** ``0,5·y(t) + 0,5·y(t+h−24)``.

Skill = ``1 − MAE_Modell / MAE_B0`` (> 0 = besser als Persistence).

Digital-Targets (An-Quotient 0..1, ``cross_entropy``): Das Modell liefert einen
*Erwartungswert* (z. B. 0,3 = „30 % der Stunde an“), die MAE belohnt dagegen
0/1-Mediane und bevorzugt systematisch die Persistence. Für Digital-Targets ist
daher ``rmse`` (Brier-artig) die maßgebliche Metrik (``skill_b0_rmse``).
"""
from __future__ import annotations

import numpy as np

from . import config
from .data import Dataset, TargetSpec, shift


def _mae(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) == 0:
        return float("nan")
    return float(np.mean(np.abs(a - b)))


def _rmse(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) == 0:
        return float("nan")
    return float(np.sqrt(np.mean((a - b) ** 2)))


def _skill(m: float, b: float) -> float:
    return float(1.0 - m / b) if b and np.isfinite(b) and b > 0 else float("nan")


def baseline_preds(ds: Dataset, spec: TargetSpec) -> dict[str, np.ndarray]:
    """Absolute Baseline-Vorhersagen (ganzes Raster)."""
    cur = spec.base
    out = {"b0": cur.copy()}
    last_col = f"{spec.target}_last"
    if last_col in ds.raw:
        out["b0_last"] = np.where(np.isfinite(ds.raw[last_col]), ds.raw[last_col], cur)
    yday = shift(ds.raw[spec.target], spec.h - 24)
    out["b2"] = np.where(np.isfinite(yday), 0.5 * cur + 0.5 * yday, cur)
    return out


def score(
    ds: Dataset,
    spec: TargetSpec,
    pred_abs: np.ndarray,
    split: str,
) -> dict[str, float]:
    """MAE Modell + Baselines auf den gültigen Zeilen von ``split``.

    ``pred_abs`` = absolute Modellvorhersage für **alle** Raster-Zeilen (NaN
    außerhalb erlaubt).
    """
    rows = ds.rows(split) & spec.mask & np.isfinite(pred_abs)
    y = spec.y_abs[rows]
    p = pred_abs[rows]
    res: dict[str, float] = {"n": int(rows.sum()), "mae": _mae(p, y), "rmse": _rmse(p, y)}
    for k, b in baseline_preds(ds, spec).items():
        res[f"mae_{k}"] = _mae(b[rows], y)
        res[f"rmse_{k}"] = _rmse(b[rows], y)
    res["skill_b0"] = _skill(res["mae"], res["mae_b0"])
    res["skill_b0_rmse"] = _skill(res["rmse"], res["rmse_b0"])
    res["max_abs_err"] = float(np.max(np.abs(p - y))) if len(y) else float("nan")
    res["max_abs_pred"] = float(np.max(np.abs(p))) if len(p) else float("nan")
    res["max_abs_y"] = float(np.max(np.abs(y))) if len(y) else float("nan")
    return res


def exploded(r: dict[str, float]) -> bool:
    """Explosion = |Vorhersage| > 1,5 × max|Ist| + 10 auf denselben Zeilen.

    Relativ statt fester 150-°C-Grenze: Abgas (`flue`) erreicht real > 150 °C.
    """
    mp, my = r.get("max_abs_pred"), r.get("max_abs_y")
    if mp is None or my is None or not (np.isfinite(mp) and np.isfinite(my)):
        return False
    return bool(mp > 1.5 * my + 10.0)


def dod_check(metrics: dict[str, dict[str, dict[str, dict[str, float]]]], model: str) -> dict:
    """DoD v002 auf Valid 2025 (Test nur berichtet).

    ``metrics[model][f"{target}@{h}"][split]`` = Ergebnis von :func:`score`.
    """
    m = metrics[model]
    h = config.DOD_HORIZON
    out: dict = {"model": model, "horizon_h": h, "ras": {}, "buff": {}}
    ras_ok = True
    for t in config.RAS_IST_TARGETS:
        r = m.get(f"{t}@{h}", {}).get("valid")
        if not r:
            ras_ok = False
            continue
        ok = bool(r["mae"] < r["mae_b0"] and r["mae"] < config.DOD_RAS_MAE_MAX)
        out["ras"][t] = {"mae": r["mae"], "mae_b0": r["mae_b0"], "ok": ok}
        ras_ok &= ok
    buff_ok = True
    for t in config.BUFF_TARGETS:
        r = m.get(f"{t}@{h}", {}).get("valid")
        if not r:
            buff_ok = False
            continue
        ok = bool(r["mae"] < config.DOD_BUFF_MAE_MAX)
        out["buff"][t] = {"mae": r["mae"], "mae_b0": r["mae_b0"], "ok": ok}
        buff_ok &= ok
    analog = [(k, s, v[s]) for k, v in m.items() for s in v
              if config.TARGET_KIND.get(k.split("@")[0]) == "analog"]
    max_pred = max((r["max_abs_pred"] for _, _, r in analog if np.isfinite(r["max_abs_pred"])),
                   default=float("nan"))
    boom = sorted({k for k, _, r in analog if exploded(r)})
    # Raum/Puffer zusätzlich gegen die physikalische 150-°C-Grenze (DoD-P0).
    dod_keys = {f"{t}@{h}" for t in config.RAS_TARGETS + config.BUFF_TARGETS}
    phys = [r["max_abs_pred"] for k, _, r in analog if k in dod_keys and np.isfinite(r["max_abs_pred"])]
    out["ras_ok"] = bool(ras_ok)
    out["buff_ok"] = bool(buff_ok)
    out["max_abs_pred"] = float(max_pred)
    out["exploded"] = boom
    out["no_explosion"] = bool(analog) and not boom and (not phys or max(phys) < config.PHYS_ABS_MAX)
    return out


def mean_skill(metrics_model: dict, split: str = "valid", targets: tuple[str, ...] | None = None) -> float:
    """Mittlerer Skill vs. B0 über Analog-Targets × Horizonte (Gewinner-Kriterium)."""
    targets = targets or config.ANALOG_TARGETS
    vals = [
        v[split]["skill_b0"]
        for k, v in metrics_model.items()
        if k.split("@")[0] in targets and split in v and np.isfinite(v[split]["skill_b0"])
    ]
    return float(np.mean(vals)) if vals else float("nan")
