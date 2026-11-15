"""Open-Meteo-Client: ERA5-Archive + Historical-Forecast (ICON-D2) + Sun-Daily.

Design-Entscheidungen (s. REPORT.md/SCHEMA.md):
- **Alle Requests in UTC** (``timezone=UTC``): Open-Meteo ist bei IANA-Zonen
  DST-unsicher ('Europe/Berlin' liefert konstant GMT+1, auch im Sommer).
  Konvertierung nach Europe/Berlin erfolgt lokal (zoneinfo).
- **Roh-Cache** ``data/features/wx_cache/<api>/<year>-<month>.json`` (RAW-API-Antwort,
  separater Raum vom Hive-Store). Resume: Datei existiert + valide Größe → skip.
- **Fair-Use**: sequentiell, ~2 s Sleep zwischen Calls, Retry mit Backoff (max 3).
- Sun: ``daily=sunrise,sunset`` wird IN DEN gleichen Call gefaltet (0 Extra-Requests).

WICHTIG: Kein CMI-Zugriff; einzige erlaubte Netzwerk-Calls = Open-Meteo-APIs.
"""
from __future__ import annotations

import calendar
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .calendar_utils import BERLIN


@dataclass
class FetchStats:
    """Zähler für den Report (Requests, Retries, Fehlschläge, Skips, fehlende Monate)."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    skipped_cached: int = 0
    missing_months: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "skipped_cached": self.skipped_cached,
            "missing_months": self.missing_months,
        }


def month_bounds(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def cache_path(cache_dir: Path, api_name: str, year: int, month: int) -> Path:
    return cache_dir / api_name / f"{year:04d}-{month:02d}.json"


def is_valid_cache(p: Path, min_size: int = 200) -> bool:
    """Cache-Datei gilt, wenn sie existiert, nicht zu klein ist und JSON-Validierung besteht."""
    if not p.exists() or p.stat().st_size < min_size:
        return False
    try:
        with p.open("rb") as f:
            data = json.load(f)
        return isinstance(data, dict) and "hourly" in data
    except (json.JSONDecodeError, OSError):
        return False


def build_url(
    base: str,
    start: str,
    end: str,
    model: str,
    lat: float,
    lon: float,
    hourly_vars: list[str],
    daily_vars: list[str] | None = None,
) -> str:
    """Open-Meteo-Query-String (UTC, non-commercial Fair-Use)."""
    params = {
        "latitude": f"{lat}",
        "longitude": f"{lon}",
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(hourly_vars),
        "models": model,
        "timezone": config.WX_TZ,
    }
    if daily_vars:
        params["daily"] = ",".join(daily_vars)
    return f"{base}?{urllib.parse.urlencode(params)}"


def http_get_json(url: str, timeout: float = config.HTTP_TIMEOUT_SEC) -> dict[str, Any]:
    """GET + JSON-Decode. Wirft bei Fehlschlag (HTTPError, URLError, Timeout)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cmi-m1-feature-build/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_month(
    base: str,
    api_name: str,
    year: int,
    month: int,
    model: str,
    cache_dir: Path,
    stats: FetchStats,
    lat: float = config.LAT,
    lon: float = config.LON,
    hourly_vars: list[str] | None = None,
    daily_vars: list[str] | None = None,
    sleep_sec: float = config.FAIR_USE_SLEEP_SEC,
    retries: int = config.MAX_RETRIES,
    backoff_base: float = config.RETRY_BACKOFF_BASE_SEC,
    force: bool = False,
    url_for: Callable | None = None,
    get_json: Callable | None = None,
) -> dict[str, Any] | None:
    """Einen Monats-Chunk laden (Cache → HTTP mit Retry/Backoff).

    Returns das geparste Daten-Dict oder None (Fehlmonat → dokumentiert, nicht stumm).
    ``url_for``/``get_json`` sind Test-Doppel (Mock, keine echten Calls in Tests).
    """
    hourly_vars = hourly_vars if hourly_vars is not None else config.WX_HOURLY_VARS
    url_fn = url_for or (lambda b, s, e, m, la, lo, hv, dv: build_url(b, s, e, m, la, lo, hv, dv))
    get_fn = get_json or http_get_json

    p = cache_path(cache_dir, api_name, year, month)
    if not force and is_valid_cache(p):
        stats.skipped_cached += 1
        with p.open("rb") as f:
            return json.load(f)

    start, end = month_bounds(year, month)
    url = url_fn(base, start, end, model, lat, lon, hourly_vars, daily_vars)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        stats.requests += 1
        try:
            data = get_fn(url)
            if not isinstance(data, dict) or ("hourly" not in data and "error" in data):
                raise ValueError(f"Open-Meteo-Fehlerantwort: {data}")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            tmp.replace(p)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            return data
        except Exception as exc:  # noqa: BLE001 - alle Call-Fehler → Retry
            last_err = exc
            if attempt < retries:
                stats.retries += 1
                time.sleep(backoff_base * (2**attempt))
    # Fehlmonat dokumentieren (nicht stumm ignorieren)
    stats.failures += 1
    stats.missing_months.append(f"{year:04d}-{month:02d} ({api_name}: {last_err})")
    return None


def hours_in_month(data: dict[str, Any] | None) -> int:
    """Anzahl der gelieferten Stunden (0 bei leerem/fehlerhaftem Chunk)."""
    if data is None:
        return 0
    t = data.get("hourly", {}).get("time")
    return len(t) if isinstance(t, list) else 0


def utc_hour(ts: str) -> datetime:
    """Open-Meteo-ISO ('YYYY-MM-DDTHH:00', UTC) → aware UTC-datetime."""
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)


def utc_hours_list(data: dict[str, Any]) -> list[datetime]:
    return [utc_hour(t) for t in data["hourly"]["time"]]


def utc_to_berlin_minute(h: datetime) -> int:
    """Aware-UTC-Datetime → Minuten ab Mitternacht Europe/Berlin.

    Kann außerhalb 0..1439 liegen (z.B. -60 = 23:00 Berlin des Vortags bei
    Sommerzeit-Übergang) — Feature-Builder normalisiert pro (Tag,Minute).
    """
    local = h.astimezone(BERLIN)
    day_start = datetime(local.year, local.month, local.day, tzinfo=BERLIN)
    return int((local - day_start) // timedelta(minutes=1))


def wx_is_forecast(hour_utc: datetime) -> int:
    """Flag: 1 = archivierte Prognose ab ICON-D2-Archivstart (2022-11-14), 0 = ERA5-Ist."""
    return int(hour_utc >= datetime(2022, 11, 14, tzinfo=UTC))


def wx_source(hour_utc: datetime) -> str:
    """Quelle-Label: 'icon_d2' (ab 2022-11-14) sonst 'era5'."""
    return "icon_d2" if hour_utc >= datetime(2022, 11, 14, tzinfo=UTC) else "era5"
