"""Wetter-Backfill: Open-Meteo-APIs monatsweise laden (Fair-Use, Resume, Retry).

Aufruf (CLI): ``python -m analysis.model.m1 weather [--force]`` (via ``m1.build``).
- ERA5 (``archive-api``): 2020-08-01 → 2022-11-13.
- Historical-Forecast (``historical-forecast-api``, ICON-D2): 2022-11-14 → Ende
  der Winsol-/CMI-Daten (Default 2026-10-31).
- Sun (daily sunrise/sunset) wird in denselben Calls mitgeliefert (0 Extra-Requests).
- Roh-Cache: ``data/features/wx_cache/{era5,hfa}/YYYY-MM.json`` (RAW-Antwort).
- Resume: valide Cache-Datei → skip. Fehlmonate: dokumentiert (stats.missing_months),
  NICHT stumm ignoriert.
- Fair-Use: sequentiell, Sleep zwischen Calls, Retry mit Backoff (max 3).

WICHTIG: Nur Open-Meteo-APIs (kein CMI-Zugriff). Timezone-Requests = UTC
(Open-Meteo IANA-ZZ ist DST-unsicher; s. config.WX_TZ-Kommentar).
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .weather import FetchStats, cache_path, fetch_month, is_valid_cache


def era5_month_range() -> list[tuple[int, int]]:
    return config.month_range(config.ERA5_BACKFILL_START, config.ERA5_BACKFILL_END)


def hfa_month_range(fetch_end: str) -> list[tuple[int, int]]:
    """ICON-D2-Monate von HFC_START bis ``fetch_end`` (monatsweise, inklusiv)."""
    start = config.HFC_START[:7] + "01"  # 2022-11-01
    return config.month_range(start, fetch_end)


def _era5_months_to_fetch(fetch_end: str) -> list[tuple[int, int]]:
    """ERA5 nur bis HFC_START (davor ICON-D2); HFC-Start-Monat geteilt:
    ERA5 trägt 2022-11-01..13, ICON-D2 ab 2022-11-14 (Split im Feature-Store)."""
    return era5_month_range()


def run_backfill(
    cfg: config.BuildConfig | None = None,
    force: bool = False,
    sleep_sec: float | None = None,
    url_for=None,
    get_json=None,
    log=print,
    retries: int = config.MAX_RETRIES,
    backoff_base: float = config.RETRY_BACKOFF_BASE_SEC,
) -> FetchStats:
    """Führt den kompletten Wetter-Backfill aus (ERA5 + HFA).

    ``url_for``/``get_json`` = Test-Doppel (Mock, keine echten Calls in Tests).
    ``retries``/``backoff_base``: Retry-Strategie (Default = Fair-Use-Produktion).
    """
    cfg = cfg or config.BuildConfig()
    stats = FetchStats()
    sun_vars = config.SUN_DAILY_VARS
    cache_dir = cfg.wx_cache_dir

    log(f"[wx] ERA5: {config.ERA5_BACKFILL_START} → {config.ERA5_BACKFILL_END} "
        f"({len(era5_month_range())} Monate)")
    for y, m in era5_month_range():
        before = stats.requests
        fetch_month(
            base=config.ERA5_ARCHIVE_URL,
            api_name="era5",
            year=y,
            month=m,
            model=config.ERA5_MODEL,
            cache_dir=cache_dir,
            stats=stats,
            daily_vars=sun_vars,
            sleep_sec=cfg.fair_use_sleep if sleep_sec is None else sleep_sec,
            force=force,
            retries=retries,
            backoff_base=backoff_base,
            url_for=url_for,
            get_json=get_json,
        )
        _log_month(log, cache_dir, y, m, "era5", stats, before)

    log(f"[wx] ICON-D2 (historical-forecast): {config.HFC_START} → {cfg.fetch_end} "
        f"({len(hfa_month_range(cfg.fetch_end))} Monate)")
    for y, m in hfa_month_range(cfg.fetch_end):
        before = stats.requests
        fetch_month(
            base=config.HIST_FCST_URL,
            api_name="hfa",
            year=y,
            month=m,
            model=config.HFC_MODEL,
            cache_dir=cache_dir,
            stats=stats,
            daily_vars=sun_vars,
            sleep_sec=cfg.fair_use_sleep if sleep_sec is None else sleep_sec,
            force=force,
            retries=retries,
            backoff_base=backoff_base,
            url_for=url_for,
            get_json=get_json,
        )
        _log_month(log, cache_dir, y, m, "hfa", stats, before)

    if stats.missing_months:
        log(f"[wx] FEHLENDE MONATE ({len(stats.missing_months)}):")
        for mm in stats.missing_months:
            log(f"   - {mm}")
    else:
        log("[wx] alle Monate geladen (oder gecacht).")
    log(
        f"[wx] Done: requests={stats.requests} retries={stats.retries} "
        f"failures={stats.failures} cached={stats.skipped_cached}"
    )
    return stats


def _log_month(log, cache_dir, y: int, m: int, api: str, stats: FetchStats, before: int) -> None:
    key = f"{y:04d}-{m:02d}"
    p = cache_path(cache_dir, api, y, m)
    state = "cached" if is_valid_cache(p) else ("ok" if stats.requests > before else "FAILED")
    log(f"   {api} {key}: {state}")


def cache_summary(cache_dir: Path | None = None) -> dict:
    """Übersicht über den Roh-Cache (für REPORT.md).

    ``cache_dir``: Basis-Verzeichnis (Default = ``config.WX_CACHE_DIR``).
    """
    base = cache_dir or config.WX_CACHE_DIR
    out: dict = {}
    for api in ("era5", "hfa"):
        d = base / api
        files = sorted(d.glob("*.json")) if d.exists() else []
        ok = [f for f in files if is_valid_cache(f)]
        out[api] = {"files": len(files), "valid": len(ok), "missing": [f.name for f in files if not is_valid_cache(f)]}
    return out


__all__ = [
    "era5_month_range",
    "hfa_month_range",
    "run_backfill",
    "cache_summary",
]
