"""Lag-Berechnung (stündlich, Kalenderstunden, DST-safe, reines Python).

Semantik (s. SCHEMA.md):
- Zeitachse = Kalendertag + Stunde (Wallclock Europe/Berlin), **keine** wall-clock
  Arithmetic über DST-Grenzen: ein Lag ``k`` Stunden = ``k`` Index-Schritte auf
  dem Stunden-Raster (``iter_hours``), das die Berliner Kalenderstunde abbildet.
- DST-Fallback-Tag (z.B. 2024-03-31 02:00 ist eine real 60-minütige Stunde,
  die im Raster genau EIN Bucket trägt) → Lags überspringen korrekt (kein
  verschieben/verdoppeln).
- Rand: am Beginn (vor dem ersten Raster-Punkt) ist der Lag ``None`` (keine
  Extrapolation).

Einsatz: Feature-Store (``lags.build_lag_arrays``) + Unit-Tests (Randfälle).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from . import config

LAG_HOURS: tuple[int, ...] = tuple(config.LAG_HOURS)


def lag_index(n_hours: int, k: int) -> int:
    """Index des Lags ``k`` für den Punkt ``i``: ``i - k`` (0-basiert).

    Returns -1 (kein Lag vorhanden) bei ``i < k``.
    """
    idx = n_hours - 1 - k
    return idx


def build_lag_arrays(values: Sequence[float | None], hours: int | None = None) -> list[list[float | None]]:
    """Erzeugt für jede Lag-Größe (1/2/6/12/24 h) ein Array mit den Lag-Werten.

    ``values[i]`` = Wert der i-ten Kalenderstunde (chronologisch, lückenloses Raster
    vorausgesetzt — Lücken als None erlaubt, Lags greifen trotzdem auf den Index).
    Returns Liste im Umfang ``LAG_HOURS`` (jeweils Array gleicher Länge).
    """
    n = len(values)
    out: list[list[float | None]] = []
    for k in LAG_HOURS:
        arr: list[float | None] = [None] * n
        for i in range(k, n):
            arr[i] = values[i - k]
        out.append(arr)
    return out


def lag_series(hours: Iterable[datetime], values: Sequence[float | None], k: int) -> list[float | None]:
    """Lag-k-Serie über einem expliziten Stunden-Raster (Validierung/Tests).

    ``hours`` muss ``values`` 1:1 entsprechen (gleiche Länge). Returns die
    Lag-Werte (``None`` für die ersten ``k`` Positionen).
    """
    hours = list(hours)
    n = len(values)
    if len(hours) != n:
        raise ValueError(f"hours ({len(hours)}) != values ({n})")
    out: list[float | None] = [None] * n
    for i in range(k, n):
        out[i] = values[i - k]
    return out


def cross_check_raster(hours: Sequence[datetime]) -> bool:
    """Prüft, ob das Stunden-Raster lückenlos ist (Δ = 1 h Kalendernachfolger).

    True = Raster ist fortlaufend (DST-Fallback-Tag zählt 1 Bucket, kein Duplikat).
    """
    # Bewusst OHNE strict=True: hours[1:] ist exakt um 1 kürzer (Paar-Prüfung).
    for a, b in zip(hours, hours[1:]):  # noqa: B905
        # Kalendernachfolger: (a + 1 h) im selben Kalender = exakt ``b`` (wall-clock).
        expected = _next_hour(a)
        if expected != b:
            return False
    return True


def _next_hour(h: datetime) -> datetime:
    """Kalender-Nachfolgerstunde (wall-clock, Europe/Berlin-semantik)."""
    return h + timedelta(hours=1)


def first_full_hour_index(hours: Sequence[datetime], ts: datetime) -> int:
    """Erster Raster-Index mit ``hours[i] <= ts`` (Binary-Suche). -1 falls vor allen."""
    lo, hi = 0, len(hours) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if hours[mid] <= ts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans
