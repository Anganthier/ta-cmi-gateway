"""Kalender-Features: hour/dow/weekend, Feiertag NRW, DST, Sonnenauf-/-untergang.

Zeitzone: Europe/Berlin (zoneinfo).

Sonnenauf-/-untergang: geschlossene Form (Meeus/NOAA low-precision, alt = -0.833°
= Standard-Sunrise/Sunset mit Brechungskorrektur). Wahl-Begründung (s. SCHEMA.md):
- PyEphem 4.2.1 (im venv) hat die ``next_*``/``rise_set``-Funktionen entfernt
  (nur ``Sun().compute()`` + manuelles Scannen möglich) → nicht produktions-tauglich.
- Open-Meteo daily-API liefert sunrise/sunset korrekt, ABER nur in UTC (IANA-Parameter
  'Europe/Berlin' ist DST-unsicher, s. config.WX_TZ-Kommentar) und bindet die
  Sun-Features an den Wetter-Cache (fehlender Monat → kein Sun).
- Geschlossene Form = deterministisch, lokal, ~µs pro Tag; gegen ein ephem-Altituden-Scan
  und die Open-Meteo-API validiert (±3-4 min, tests/test_calendar_utils.py).
"""
from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays

BERLIN = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")

# NRW-Feiertage: python-holidays DE, Subdivision "NW" (Nordrhein-Westfalen).
# python-holidays erzeugt das Kalender-Objekt NUR für übergebene ``years``;
# wir decken 2020–2027 (Feature-Store 2020-08 → 2026-10 + Puffer) ab.
_NRW_YEARS = list(range(2019, 2028))
_NRW_HOLIDAYS: dict[date, str] = {
    d: name for d, name in holidays.DE(subdiv="NW", years=_NRW_YEARS).items()
}


def is_nrw_holiday(d: date) -> str | None:
    """Feiertag-Name in NRW (str) oder None."""
    return _NRW_HOLIDAYS.get(d)


@dataclass(frozen=True)
class SunTimes:
    """Sonnenauf-/untergang eines Kalendertags (Europe/Berlin)."""

    date: date
    sunrise: datetime | None  # aware, Europe/Berlin
    sunset: datetime | None


def _deg(x: float) -> float:
    return x * math.pi / 180.0


def noaa_sun_utc(day: date, lat: float = 51.382974, lon: float = 7.774286, alt: float = -0.833) -> tuple[float, float] | None:
    """Sonnenauf-/-untergang als UTC-Stunden (0..24, ggf. <0/>24) eines Tags.

    Meeus/NOAA low-precision (ausreichend für ±1-2 min hierzulande).
    Liefert None bei Polar-Nacht/-Mitternacht (hier praktisch unmöglich).
    """
    D = (day.toordinal() - date(2000, 1, 1).toordinal()) + 0.5
    L0 = 280.460 + 0.9856474 * D
    g = _deg((357.528 + 0.9856003 * D) % 360.0)
    C = 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)
    lam = _deg((L0 + C) % 360.0)
    eps = _deg(23.439 - 0.0000004 * D)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    ra = math.degrees((ra + 2 * math.pi) % (2 * math.pi))
    delta = math.asin(math.sin(eps) * math.sin(lam))
    cos_h = (
        math.sin(_deg(alt)) - math.sin(_deg(lat)) * math.sin(delta)
    ) / (math.cos(_deg(lat)) * math.cos(delta))
    if cos_h > 1.0 or cos_h < -1.0:
        return None
    H = math.degrees(math.acos(cos_h))
    x = L0 - ra
    x = (x + 180.0) % 360.0 - 180.0
    eot_min = 4.0 * x
    noon_utc_h = 12.0 - eot_min / 60.0 - lon / 15.0
    half = H / 15.0
    return noon_utc_h - half, noon_utc_h + half


def sun_times(day: date, lat: float = 51.382974, lon: float = 7.774286) -> SunTimes:
    """Sonnenauf-/untergang für ``day`` (Europe/Berlin, aware datetimes)."""
    res = noaa_sun_utc(day, lat, lon)
    if res is None:
        return SunTimes(day, None, None)
    rise_utc, set_utc = res
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    sunrise = base + timedelta(hours=rise_utc)
    sunset = base + timedelta(hours=set_utc)
    return SunTimes(day, sunrise.astimezone(BERLIN), sunset.astimezone(BERLIN))


def sun_times_bulk(days: Iterator[date]) -> Iterator[SunTimes]:
    yield from (sun_times(d) for d in days)


def is_dst(dt: datetime) -> bool:
    """DST-Flag (Sommerzeit) für einen Europe/Berlin-Datetime.

    Naive Datetimes werden implizit als Europe/Berlin interpretiert.
    """
    dt = dt.replace(tzinfo=BERLIN) if dt.tzinfo is None else dt.astimezone(BERLIN)
    return bool(dt.dst())


def calendar_features(day: date) -> dict[str, int | str]:
    """Kalender-Features eines Kalendertags (Europe/Berlin).

    - ``hour`` wird pro Stunde gesetzt (hier: -1 als Platzhalter).
    - ``day_of_week``: 0 = Mo … 6 = So.
    - ``is_weekend``: 1 = Sa/So.
    - ``is_holiday``: 1 = Feiertag (NRW).
    - ``holiday``: Name oder "none".
    - ``is_dst``: 1 = Sommerzeit.
    """
    dow = day.weekday()  # 0=Mo
    return {
        "day_of_week": dow,
        "is_weekend": 1 if dow >= 5 else 0,
        "is_holiday": 1 if is_nrw_holiday(day) is not None else 0,
        "holiday": is_nrw_holiday(day) or "none",
        "is_dst": 1 if is_dst(datetime(day.year, day.month, day.day, tzinfo=BERLIN)) else 0,
    }


def sun_features(day: date) -> dict[str, int | str]:
    """Sunrise/Sunset (Minute-of-day, integer) + Dauer in Minuten."""
    st = sun_times(day)
    if st.sunrise is None or st.sunset is None:
        return {"sunrise_min": -1, "sunset_min": -1, "daylight_min": -1}
    rise = st.sunrise.time()
    set_ = st.sunset.time()
    # Edge-Fall: Sunset "nächsten" Tages (nur bei polaren Extremwerten) → -1.
    rise_min = rise.hour * 60 + rise.minute
    set_min = set_.hour * 60 + set_.minute
    if set_min <= rise_min:
        return {"sunrise_min": -1, "sunset_min": -1, "daylight_min": -1}
    return {"sunrise_min": rise_min, "sunset_min": set_min, "daylight_min": set_min - rise_min}


def day_is_dst(day: date) -> bool:
    """DST-Flag für einen Kalendertag (Berlin): True, wenn um 12:00 DST gilt."""
    return bool(datetime(day.year, day.month, day.day, 12, 0, tzinfo=BERLIN).dst())


def first_full_hour(start: date) -> datetime:
    """Erste vollen Kalenderstunde ab ``start`` (00:00, aware Europe/Berlin).

    Für M1: ``first_full_hour(2020-08-31)`` = 2020-08-31 00:00 Europe/Berlin
    (Logger startete 2020-08-30 12:36 → erste volle Stunde = nächster Tag).
    """
    return datetime(start.year, start.month, start.day, tzinfo=BERLIN)


def iter_hours(start: datetime, end: datetime) -> Iterator[datetime]:
    """Alle Kalenderstunden [start, end] (start = volle Stunde, inclusive)."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        yield cur
        cur += timedelta(hours=1)
