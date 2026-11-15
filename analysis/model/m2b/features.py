"""M2b — Feature-Ableitung (rein funktional: Rohspalten → Matrix).

``derive(raw, ts, names=None)`` ist die **einzige** Stelle, an der aus Rohspalten
die Modell-Matrix entsteht — Training, Vorhersage (``m2b.predict``) und der
Responsivitäts-Test (``m2b.responsivity``: Offset + perzipiert verschieben,
dann neu ableiten) nutzen dieselbe Funktion. Damit bleiben abgeleitete
Größen (z. B. ``ras_perc_min``) bei Szenario-Eingriffen konsistent.

``names`` (v003): genau diese Spalten in dieser Reihenfolge (Rohspalte oder
abgeleitet, Ableitung nur bei Bedarf). So lädt und rechnet das v002-Artefakt
(``m2b-f2``-Namen aus ``features.json``) mit demselben Code weiter.
Unbekannter Name → ``KeyError``.

Abgeleitete Features ``m2b-f2`` (Begründung in ``docs/l1-model-v002.md`` §3):
- ``ras_perc_min`` / ``ras_perc_min_last`` / ``ras_ist_min``: Regelung reagiert
  auf den **kältesten** Raum (UVR1611: RT = MIN der perzipierten Raumwerte,
  ``docs/uvr-programm-analyse.md``).
- ``{c}_d1`` = ``c − c_lag1`` (1-h-Änderung, Trend für Bäume explizit).
- ``{c}_trend`` = ``c_last − c`` (Trend innerhalb der Stunde; Stundenmittel
  hinkt ~30 min hinterher).
- ``wxa_t_mean_{w}`` / ``wxa_t_min_{w}`` / ``wxa_swr_sum_{w}`` / ``wxa_cc_mean_{w}``:
  Wetter-Aggregate der nächsten ``w`` Stunden (passend zu den Horizonten).
- ``doy_sin`` / ``doy_cos`` / ``hour_sin`` / ``hour_cos``: Saison + Tageszeit zyklisch.
- ``rt_soll`` / ``rt_err`` / ``rt_err_last``: Raum-Soll des Heizkreis-Reglers laut
  Zeitprogramm (20 °C 06:00–22:30, sonst 16 °C; Stunde 22 = anteilig 18 °C) und
  Regelabweichung ``rt_soll − ras_perc_min`` (> 0 = zu kalt ⇒ Heizbedarf). Ein
  Offset-Schritt in der kältesten Zone verschiebt ``rt_err`` direkt um −Δ.

Neu in ``m2b-f3`` (v003, ``docs/l1-model-v003.md`` §4; Schwellen aus
``docs/uvr-programm-analyse.md`` §2.2/§2.3, s. ``config.UVR_*``), > 0 = Schaltbedingung erfüllt:
- ``uvr_o9_margin`` = buff_mid − hz_rl − 5 K (O9 Heizungs-RL in den Puffer),
- ``uvr_o2_margin`` = oil_vl − buff_top − 24,6 K (O2 Öl lädt Puffer oben),
- ``uvr_ww_margin`` = 57 °C − buff_top (WW-Anforderung → Ölbrenner O5),
- ``uvr_o1_margin`` = min(wood_vl − 65, wood_vl − buff_bot − 3) (O1 Holzpumpe),
  jeweils auch ``*_last`` aus den letzten 10-s-Werten der Stunde.
- ``vlst_est`` = Heizkurve (+10 °C → 45 °C, −20 °C → 65 °C, linear, geklemmt 25..70)
  aus ``ext``; ``vl_err`` = ``vlst_est − hz_vl`` (> 0 = Vorlauf unter Soll).
- ``buff_strat_top_mid`` / ``buff_strat_mid_bot``: Pufferschichtung.
- ``hz_flow_pump`` / ``hz_flow_o9``: Wärmestrom-Proxy (hz_vl − hz_rl) × An-Quotient
  Heizkreispumpe bzw. O9-Ventil.
"""
from __future__ import annotations

import warnings
from collections.abc import Callable

import numpy as np

from . import config

DERIVE_VERSION = "m2b-f3"


def _nanmin(*arrs: np.ndarray) -> np.ndarray:
    stack = np.stack(arrs, axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # All-NaN-Spalten → NaN
        return np.nanmin(stack, axis=0)


def _nanmean(stack: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(stack, axis=0)


class _Cols:
    """Rohspalten + abgeleitete Spalten, Ableitung erst bei Bedarf (mit Cache)."""

    def __init__(self, raw, ts: np.ndarray):
        self.raw = raw
        self.ts = np.asarray(ts).astype("datetime64[h]")
        self._cache: dict[str, np.ndarray] = {}

    def __call__(self, name: str) -> np.ndarray:
        v = self._cache.get(name)
        if v is None:
            if name in _DERIVED:
                v = _DERIVED[name](self)
            elif name in self.raw:
                v = np.asarray(self.raw[name], dtype=np.float64)
            else:
                raise KeyError(f"unbekanntes Feature {name!r} (weder Rohspalte noch abgeleitet)")
            self._cache[name] = v
        return v

    @property
    def hour(self) -> np.ndarray:
        return (self.ts - self.ts.astype("datetime64[D]")).astype(np.float64)

    @property
    def doy(self) -> np.ndarray:
        return (self.ts.astype("datetime64[D]") - self.ts.astype("datetime64[Y]")).astype(np.float64)


_DERIVED: dict[str, Callable[[_Cols], np.ndarray]] = {}
# Namen je Ableitungs-Version (Reihenfolge = Spaltenreihenfolge des Default-derive).
DERIVED_F2: list[str] = []
DERIVED_F3: list[str] = []


def _reg(target: list[str], name: str, fn: Callable[[_Cols], np.ndarray]) -> None:
    _DERIVED[name] = fn
    target.append(name)


def _wx(g: _Cols, key: str, w: int) -> np.ndarray:
    return np.stack([g(f"wx_t{k}_{key}") for k in range(w)], axis=0)


def _register() -> None:
    z = config.ZONES
    f2, f3 = DERIVED_F2, DERIVED_F3
    _reg(f2, "ras_perc_min", lambda g: _nanmin(*(g(f"ras_{q}_perc") for q in z)))
    _reg(f2, "ras_perc_min_last", lambda g: _nanmin(*(g(f"ras_{q}_perc_last") for q in z)))
    _reg(f2, "ras_ist_min", lambda g: _nanmin(*(g(f"ras_{q}_ist") for q in z)))
    for c in config.DIFF_COLS:
        _reg(f2, f"{c}_d1", lambda g, c=c: g(c) - g(f"{c}_lag1"))
    for c in config.LAST_COLS:
        _reg(f2, f"{c}_trend", lambda g, c=c: g(f"{c}_last") - g(c))
    for w in config.WX_AGG_WINDOWS:
        _reg(f2, f"wxa_t_mean_{w}", lambda g, w=w: _nanmean(_wx(g, "t_bias", w)))
        _reg(f2, f"wxa_t_min_{w}", lambda g, w=w: _nanmin(*_wx(g, "t_bias", w)))
        _reg(f2, f"wxa_swr_sum_{w}", lambda g, w=w: np.nansum(_wx(g, "swr", w), axis=0))
        _reg(f2, f"wxa_cc_mean_{w}", lambda g, w=w: _nanmean(_wx(g, "cc", w)))
    _reg(f2, "doy_sin", lambda g: np.sin(2 * np.pi * g.doy / 365.25))
    _reg(f2, "doy_cos", lambda g: np.cos(2 * np.pi * g.doy / 365.25))
    _reg(f2, "hour_sin", lambda g: np.sin(2 * np.pi * g.hour / 24.0))
    _reg(f2, "hour_cos", lambda g: np.cos(2 * np.pi * g.hour / 24.0))
    _reg(f2, "rt_soll", lambda g: rt_soll(g.hour))
    _reg(f2, "rt_err", lambda g: g("rt_soll") - g("ras_perc_min"))
    _reg(f2, "rt_err_last", lambda g: g("rt_soll") - g("ras_perc_min_last"))
    # --- m2b-f3 (v003) ---
    for sfx in ("", "_last"):
        _reg(f3, f"uvr_o9_margin{sfx}",
             lambda g, s=sfx: g(f"buff_mid{s}") - g(f"hz_rl{s}") - config.UVR_O9_DIFF_K)
        _reg(f3, f"uvr_o2_margin{sfx}",
             lambda g, s=sfx: g(f"oil_vl{s}") - g(f"buff_top{s}") - config.UVR_O2_DIFF_K)
        _reg(f3, f"uvr_ww_margin{sfx}", lambda g, s=sfx: config.UVR_WW_SOLL - g(f"buff_top{s}"))
        _reg(f3, f"uvr_o1_margin{sfx}", lambda g, s=sfx: np.minimum(
            g(f"wood_vl{s}") - config.UVR_O1_MIN,
            g(f"wood_vl{s}") - g(f"buff_bot{s}") - config.UVR_O1_DIFF_K))
    _reg(f3, "vlst_est", lambda g: vlst_est(g("ext")))
    _reg(f3, "vl_err", lambda g: g("vlst_est") - g("hz_vl"))
    _reg(f3, "buff_strat_top_mid", lambda g: g("buff_top") - g("buff_mid"))
    _reg(f3, "buff_strat_mid_bot", lambda g: g("buff_mid") - g("buff_bot"))
    _reg(f3, "hz_flow_pump", lambda g: (g("hz_vl") - g("hz_rl")) * g("pump_hz_on"))
    _reg(f3, "hz_flow_o9", lambda g: (g("hz_vl") - g("hz_rl")) * g("valve_hzrl_on"))


def rt_soll(hour: np.ndarray) -> np.ndarray:
    """Raum-Soll je Stunden-Bucket (zeitanteilig Norm/Absenk, s. Modul-Doku)."""
    h = np.asarray(hour, dtype=np.float64)
    day = np.clip(np.minimum(h + 1.0, config.RT_DAY_END_H) - np.maximum(h, config.RT_DAY_START_H), 0.0, 1.0)
    return config.RT_ABSENK + (config.RT_NORM - config.RT_ABSENK) * day


def vlst_est(ext: np.ndarray) -> np.ndarray:
    """Vorlauf-Soll laut Heizkurve (linear durch ``config.HK_CURVE``, geklemmt VLMin..VLMax)."""
    (x0, y0), (x1, y1) = config.HK_CURVE
    e = np.asarray(ext, dtype=np.float64)
    v = y0 + (e - x0) * (y1 - y0) / (x1 - x0)
    return np.clip(v, config.HK_VL_MIN, config.HK_VL_MAX)


_register()


def default_feature_names() -> list[str]:
    """Feature-Liste des aktuellen Modells (v003): ``RAW_INPUTS`` + alle Ableitungen."""
    return list(config.RAW_INPUTS) + list(_DERIVED)


def derived_columns(raw: dict[str, np.ndarray], ts: np.ndarray) -> dict[str, np.ndarray]:
    """Alle abgeleiteten Spalten (Name → float64-Array)."""
    g = _Cols(raw, ts)
    return {n: g(n) for n in _DERIVED}


def derive(
    raw: dict[str, np.ndarray],
    ts: np.ndarray,
    names: list[str] | tuple[str, ...] | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Rohspalten + Zeitstempel → (X float32 (n, d), Feature-Namen).

    ``names`` = gewünschte Spalten (Reihenfolge bleibt); ``None`` = :func:`default_feature_names`.
    """
    out_names = list(names) if names is not None else default_feature_names()
    g = _Cols(raw, ts)
    n = len(np.asarray(ts))
    cols = [g(c) for c in out_names]
    X = np.stack(cols, axis=1).astype(np.float32) if cols else np.empty((n, 0), dtype=np.float32)
    return X, out_names


class _Recorder(dict):
    """Pseudo-Rohdaten: jede angefragte Rohspalte wird protokolliert (für :func:`required_raw`)."""

    def __init__(self):
        super().__init__()
        self.used: list[str] = []

    def __contains__(self, key) -> bool:
        return True

    def __getitem__(self, key):
        if key not in self.used:
            self.used.append(key)
        return np.zeros(1, dtype=np.float64)


def required_raw(names: list[str] | tuple[str, ...]) -> list[str]:
    """Rohspalten, die :func:`derive` für ``names`` liest (Reihenfolge der ersten Nutzung)."""
    rec = _Recorder()
    derive(rec, np.array(["2025-01-01T00"], dtype="datetime64[h]"), names=names)
    return rec.used


def rule_state(perc: np.ndarray, prev: np.ndarray) -> np.ndarray:
    """Minderer-Regel: 1 (offen) unter, 0 (zu) über ``HZRKL_RULE_THRESHOLD``; Gleichstand/NULL → ``prev``."""
    p = np.asarray(perc, dtype=np.float64)
    prev = np.asarray(prev, dtype=np.float64)
    thr, eps = config.HZRKL_RULE_THRESHOLD, config.HZRKL_RULE_EPS
    with np.errstate(invalid="ignore"):
        return np.where(p < thr - eps, 1.0, np.where(p > thr + eps, 0.0, prev))


def apply_offset_step(raw: dict[str, np.ndarray], zone: str, delta: float) -> dict[str, np.ndarray]:
    """Szenario „RAS-Offset der Zone um ``delta`` K erhöhen“ (Kopie).

    Physik/Semantik (Q-100): perzipiert = Ist + Offset. Ein Offset-Schritt
    verschiebt daher Offset **und** perzipierte Temperatur (Stundenmittel und
    letzter Wert) um ``delta``; die gemessene Ist-Temperatur bleibt. Lags
    bleiben unverändert (der Schritt passiert jetzt).

    v003: Existieren ``hzrkl_{zone}_rule``/``_rule_last`` im ``raw``-Dict, wird der
    Minderer-Zustand aus ``ras_{zone}_perc_last + delta`` nach der 20,1-°C-Regel
    neu bestimmt (:func:`rule_state`, Gleichstand/NULL → bisheriger Zustand
    ``_rule_last``). Der neue Zustand gilt ab jetzt: ``_rule_last`` = neuer
    Zustand; ``_rule`` (Stundenanteil) wird nur dort auf den neuen Zustand
    gesetzt, wo er kippt — ohne Kippen bleibt das Stundenmittel (``delta = 0``
    ist damit die Identität). v002-Rohdaten (ohne Regel-Spalten) bleiben unberührt.
    """
    out = dict(raw)
    for c in (f"offset_{zone}", f"ras_{zone}_perc", f"ras_{zone}_perc_last"):
        out[c] = np.asarray(raw[c], dtype=np.float64) + float(delta)
    rule, last = f"hzrkl_{zone}_rule", f"hzrkl_{zone}_rule_last"
    if rule in raw and last in raw:
        prev = np.asarray(raw[last], dtype=np.float64)
        new = rule_state(out[f"ras_{zone}_perc_last"], prev)
        flip = np.isfinite(new) & ~(new == prev)
        out[last] = new
        out[rule] = np.where(flip, new, np.asarray(raw[rule], dtype=np.float64))
    return out
