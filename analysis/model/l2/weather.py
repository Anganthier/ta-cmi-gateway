"""L2 — LIVE-24-h-FORECAST (Open-Meteo ICON-D2, UTC, non-commercial Fair-Use).

Ergänzt M1 (ERA5 + ICON-D2-ARCHIV = historisch) um den **Live-Forecast** für
die Empfehlungse-Engine:

- Endpunkt ``api.open-meteo.com/v1/forecast`` mit ``models=icon_d2`` (2 km,
  3-h-Update), ``timezone=UTC`` (IANA-Zonen sind bei Open-Meteo DST-unsicher,
  s. M1-``config.WX_TZ``-Kommentar). Konvertierung nach Europe/Berlin erfolgt
  lokal mit ``zoneinfo`` (:func:`utc_to_berlin_hour`).
- Window je Empfehlung: Stunden ``[ts_utc .. ts_utc+23 h]`` (24 h =
  ``config.WX_HORIZON_H``), ts = volle Berliner Kalenderstunde. Die API
  liefert ganztägige Fenster (``start_date``/``end_date``); L2 schneidet das
  exakte 24-h-Window heraus (Tag(s) um ``ts`` + ggf. Folgetag).
- Cache: **rohe API-Antwort** unter ``analysis/model/l2/runs/wx_live/wx_live_
  <YYYY-MM-DD>.json`` (Tag = Tag von ``ts`` in Europe/Berlin, 1:1 wie M1-
  ``wx_cache``). Valid = existiert, JSON-Validierung, ``hourly`` vorhanden,
  Window vollständig abgedeckt (24 Stunden, alle Variablen).
- **Wetter-Run (``wx_run``)**: Die Live-API meldet keine explizite
  Modell-Run-Zeit (``generationtime_ms`` ≈ 0 = Berechnungszeit). Proxy =
  **Request-Zeit**: Frisch-Call = jetzt, Cache-Datei = Datei-MTIME (Schreib-
  zeit = Abrufzeit). Run-Proxy > ``config.WX_RUN_STALE_MAX_H`` h alt → stale
  (konservativ: 3-h-Model-Update-Zyklus ist damit abgedeckt).
- Bias-Korrektur an den CMI-Außensensor (dev0 a8): saisonales Median-Offset +
  Drift aus dem M1-Bias-Profil (gleiche Methode wie ``wx_t{k}_t_bias``).

WICHTIG: kein CMI-Zugriff; einziger erlaubter Netzwerk-Call = Open-Meteo-API.
API-Fehler/Fehlendes-Window → ``None`` (Engine: Fallback auf Store-Wetter +
``stale=true``, s. ``l2.recommend``).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass
class WxLive:
    """Geparster LIVE-Forecast (24 h ab ``ts``) + Metadaten für Quellenangabe."""

    ts: datetime  # volle Berliner Kalenderstunde der Entscheidung (aware)
    utc_hour: list[datetime]  # 24 aware-UTC-Stunden [ts_utc .. ts_utc+23]
    hourly: dict[str, list[float | None]]  # Variable → Werte (24, NaN-als-None)
    t_bias: list[float | None]  # temperature_2m + Bias-Korrektur (CMI-Sensor)
    wx_quelle: str  # "Open-Meteo ICON-D2 (live)"
    wx_run: str | None  # Run-Proxy: Request-Zeit (ISO UTC)
    wx_age_h: float | None  # Alter des Runs in Stunden (None = unbekannt)
    fetched_at: str | None = None  # Request-Zeit (ISO UTC; Cache: MTIME)
    extra: dict[str, Any] = field(default_factory=dict)  # z. B. generationtime_ms


def berlin_hour(ts: datetime | str) -> datetime:
    """'YYYY-MM-DD HH:MM' bzw. datetime → volle Berliner Kalenderstunde (aware)."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=BERLIN)
    return ts.astimezone(BERLIN).replace(minute=0, second=0, microsecond=0)


def window_utc(ts: datetime, horizon_h: int = config.WX_HORIZON_H) -> list[datetime]:
    """Aware-UTC-Stunden des Forecast-Fensters: ``ts_utc`` bis ``ts_utc+(h-1)``."""
    start = ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [start + timedelta(hours=k) for k in range(int(horizon_h))]


def window_days(ts: datetime, horizon_h: int = config.WX_HORIZON_H) -> tuple[str, str]:
    """(start_date, end_date) für den API-Call (ganztägige Fenster, UTC-Daten)."""
    hours = window_utc(ts, horizon_h)
    return hours[0].strftime("%Y-%m-%d"), hours[-1].strftime("%Y-%m-%d")


def cache_path(ts: datetime, runs_dir: Path | None = None) -> Path:
    """Cache-Pfad (Tag von ``ts`` in Europe/Berlin): ``wx_live_<YYYY-MM-DD>.json``."""
    return Path(runs_dir or config.WX_LIVE_DIR) / f"wx_live_{ts.astimezone(BERLIN):%Y-%m-%d}.json"


def build_url(
    ts: datetime,
    model: str = config.FORECAST_MODEL,
    lat: float = config.LAT,
    lon: float = config.LON,
    hourly_vars: list[str] | None = None,
    horizon_h: int = config.WX_HORIZON_H,
) -> str:
    """Open-Meteo-Forecast-Query (UTC, Non-commercial; Tages-Fenster um ``ts``)."""
    start_d, end_d = window_days(ts, horizon_h)
    params = {
        "latitude": f"{lat}",
        "longitude": f"{lon}",
        "start_date": start_d,
        "end_date": end_d,
        "hourly": ",".join(hourly_vars if hourly_vars is not None else config.WX_HOURLY_VARS),
        "models": model,
        "timezone": config.WX_TZ,
    }
    return f"{config.FORECAST_URL}?{urllib.parse.urlencode(params)}"


def http_get_json(url: str, timeout: float = config.HTTP_TIMEOUT_SEC) -> dict[str, Any]:
    """GET + JSON-Decode. Wirft bei Fehlschlag (HTTPError, URLError, Timeout)."""
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_run_time(data: dict[str, Any]) -> float | None:
    """``generationtime_ms`` (Berechnungszeit der Antwort, s) — Referenz nur.

    Die Live-API liefert keine explizite Modell-Run-Zeit (Live: ≈ 0 s).
    Run-Proxy = Request-Zeit (s. :func:`fetch_live`); dieser Wert wird nur
    als Metadatum mitgeführt (``WxLive.extra``).
    """
    gen_ms = data.get("generationtime_ms")
    if not isinstance(gen_ms, (int, float)) or gen_ms < 0:
        return None
    return float(gen_ms) / 1000.0


def load_bias_profile(path: Path | None = None) -> dict[str, Any] | None:
    """M1-Bias-Profil laden (fehlende Datei → None = keine Bias-Korrektur)."""
    p = Path(path) if path is not None else config.BIAS_PROFILE_PATH
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def season_name(d: datetime) -> str:
    """Saison-Label wie M1 (``bias_terms``): DJF/MAM/JJA/SON."""
    m = d.month
    if m in (12, 1, 2):
        return "DJF"
    if m in (3, 4, 5):
        return "MAM"
    if m in (6, 7, 8):
        return "JJA"
    return "SON"


def season_start(d: datetime) -> datetime:
    """Saisonen-Startdatum (M1-``season_start_expr`` identisch)."""
    m = d.month
    y = d.year
    if m == 12:
        return datetime(y - 1, 12, 1)
    if m in (1, 2):
        return datetime(y, 1, 1)
    if m in (3, 4, 5):
        return datetime(y, 3, 1)
    if m in (6, 7, 8):
        return datetime(y, 6, 1)
    return datetime(y, 9, 1)


def bias_correct(
    ts: datetime,
    t_grid: list[float | None],
    bias_profile: dict[str, Any] | None,
) -> list[float | None]:
    """Grid-T → ``t_bias`` (M1-Methode: saisonales Median-Offset + OLS-Drift).

    Bezug = ``ts`` (Berliner Kalenderstunde der Entscheidung), identisch zu
    M1-``bias_terms`` (``g.ts``-Bezug). Keine Bias-Korrektur bei None-Profil
    (Roh-Grid-T, dokumentiert).
    """
    raw = [None if v is None else float(v) for v in t_grid]
    if not bias_profile:
        return raw
    prof = (bias_profile.get("profile") or {}).get(season_name(ts)) or {}
    off = float(prof.get("offset_k", 0.0))
    drift = float(prof.get("drift_k_per_day", 0.0))
    days = (ts.date() - season_start(ts).date()).days
    return [None if v is None else v + off + drift * days for v in raw]


def slice_window(
    data: dict[str, Any], ts: datetime, horizon_h: int = config.WX_HORIZON_H
) -> dict[str, Any]:
    """Roh-Antwort (Tages-Fenster) → exakt das 24-h-Window ab ``ts`` (UTC).

    Die API liefert ganztägig (24 h ab start_date); das exakte Fenster
    ``[ts_utc .. ts_utc+23]`` kann in 1–2 Kalendertage ragen. Fehlt eine
    Stunde (API-Lücke/fehlender Folgetag) → ``ValueError`` (keine Stille).
    """
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in data["hourly"]["time"]]
    idx = {t: i for i, t in enumerate(times)}
    want = window_utc(ts, horizon_h)
    i0 = idx.get(want[0])
    i1 = idx.get(want[-1])
    if i0 is None or i1 is None or i1 - i0 != int(horizon_h) - 1:
        raise ValueError(f"Open-Meteo-Antwort deckt das 24-h-Window ab {want[0]} nicht ab")
    # Raw-Format der API (UTC, timezone=UTC → naive-UTC-ISO ohne Offset),
    # damit Cache-Dateien und geschnittene Fenster 1:1 zur Roh-Antwort sind.
    out = {"hourly": {"time": [t.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="minutes") for t in want]}}
    for var in config.WX_HOURLY_VARS:
        vals = data["hourly"].get(var)
        if vals is None:
            continue
        out["hourly"][var] = list(vals[i0 : i1 + 1])
    for key in data:
        if key != "hourly":
            out[key] = data[key]
    return out


def window_complete(data: dict[str, Any], ts: datetime, horizon_h: int = config.WX_HORIZON_H) -> bool:
    """Antwort (ggf. bereits geschnitten) deckt das komplette 24-h-Window ab."""
    times = data.get("hourly", {}).get("time") or []
    if len(times) != int(horizon_h):
        return False
    got = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in times]
    want = window_utc(ts, horizon_h)
    return got[0] == want[0] and got[-1] == want[-1] and got[1] - got[0] == timedelta(hours=1)


def parse_response(
    data: dict[str, Any],
    ts: datetime,
    horizon_h: int = config.WX_HORIZON_H,
    bias_profile: dict[str, Any] | None = None,
    fetched_at: datetime | None = None,
    now: datetime | None = None,
) -> WxLive:
    """Roh-/geschnittene Antwort → :class:`WxLive` (validiert, exakt ``ts``-basiert).

    ``fetched_at`` = Request-Zeit (aware UTC; Run-Proxy, s. Modul-Doku).
    ``now`` = Referenz für das Run-Alter (Default: jetzt).
    """
    if not window_complete(data, ts, horizon_h):
        raise ValueError("Open-Meteo-Antwort deckt das 24-h-Window nicht vollständig ab")
    hourly: dict[str, list[float | None]] = {}
    for var in config.WX_HOURLY_VARS:
        vals = data["hourly"].get(var)
        if vals is None or len(vals) != int(horizon_h):
            raise ValueError(f"Variable {var!r} fehlt/unvollständig in der API-Antwort")
        hourly[var] = [None if v is None else float(v) for v in vals]
    fetched = fetched_at or (now or datetime.now(UTC))
    now_ = now or datetime.now(UTC)
    age_h = (now_ - fetched).total_seconds() / 3600.0
    return WxLive(
        ts=ts,
        utc_hour=window_utc(ts, horizon_h),
        hourly=hourly,
        t_bias=bias_correct(ts, hourly["temperature_2m"], bias_profile),
        wx_quelle="Open-Meteo ICON-D2 (live)",
        wx_run=fetched.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        wx_age_h=age_h,
        fetched_at=fetched.astimezone(UTC).isoformat(),
        extra={"generationtime_ms": parse_run_time(data)},
    )


def _read_cache(
    p: Path, ts: datetime, horizon_h: int, bias_profile: dict[str, Any] | None, now: datetime
) -> WxLive | None:
    if not p.exists() or p.stat().st_size < 200:
        return None
    try:
        with p.open("rb") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "hourly" not in data:
            return None
        # Cache enthält das komplette Tages-Fenster (ggf. mehr) → herausschneiden.
        try:
            data = slice_window(data, ts, horizon_h)
        except ValueError:
            if not window_complete(data, ts, horizon_h):
                return None
        fetched = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        return parse_response(data, ts, horizon_h, bias_profile, fetched_at=fetched, now=now)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def fetch_live(
    ts: datetime | str,
    runs_dir: Path | None = None,
    bias_profile: dict[str, Any] | None = None,
    horizon_h: int = config.WX_HORIZON_H,
    force: bool = False,
    url_for: Callable | None = None,
    get_json: Callable | None = None,
    retries: int = config.MAX_RETRIES,
    backoff_base: float = config.RETRY_BACKOFF_BASE_SEC,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WxLive | None:
    """LIVE-24-h-Forecast laden (Cache → HTTP mit Retry/Backoff).

    Returns :class:`WxLive` oder ``None`` (API nicht erreichbar / Antwort
    ungültig → Engine: Fallback auf Store-Wetter + ``stale=true``).
    ``url_for``/``get_json`` = Test-Doppel (keine echten Calls in Tests).
    ``clock`` = Referenzzeit (Run-Alter; Default: jetzt, UTC).
    """
    t0 = berlin_hour(ts)
    now = clock()
    p = cache_path(t0, runs_dir)
    if not force:
        cached = _read_cache(p, t0, horizon_h, bias_profile, now)
        if cached is not None:
            return cached

    url_fn = url_for or (lambda t: build_url(t, horizon_h=horizon_h))
    get_fn = get_json or http_get_json
    url = url_fn(t0)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            data = get_fn(url)
            if not isinstance(data, dict) or ("hourly" not in data and "error" in data):
                raise ValueError(f"Open-Meteo-Fehlerantwort: {data}")
            try:
                data = slice_window(data, t0, horizon_h)
            except ValueError as exc:
                raise ValueError(f"Window-Slice fehlgeschlagen: {exc}") from exc
            wx = parse_response(data, t0, horizon_h, bias_profile, fetched_at=now, now=now)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            tmp.replace(p)
            return wx
        except Exception as exc:  # noqa: BLE001 - alle Call-/Slice-/Parse-Fehler → Retry
            last_err = exc
            if attempt < retries:
                time.sleep(backoff_base * (2**attempt))
    if last_err is not None:
        print(f"[l2.weather] LIVE-Forecast nicht erreichbar ({last_err}) → Fallback", flush=True)
    return None


def utc_to_berlin_hour(h: datetime) -> int:
    """Aware-UTC-Datetime → Stunde (0..23) in Europe/Berlin (DST-korrekt)."""
    return h.astimezone(BERLIN).hour


def wx_min_12h(wx: WxLive) -> float | None:
    """Mindest-Außen-T (bias-korrigiert) in den nächsten 12 h (Frost-Gate)."""
    vals = [v for v in wx.t_bias[:12] if v is not None]
    return min(vals) if vals else None


def wx_solar_12h(wx: WxLive) -> float | None:
    """Summe der Shortwave-Strahlung (Wh/m²) in den nächsten 12 h (WW-Solar-Fenster)."""
    vals = [v for v in (wx.hourly.get("shortwave_radiation") or [])[:12] if v is not None]
    return float(sum(vals)) if vals else None


def wx_frost_night(wx: WxLive) -> bool:
    """Frost-Nacht: min. Außen-T (bias-korrigiert, 12 h) ≤ ``FROST_T_MIN_C``."""
    tmin = wx_min_12h(wx)
    return tmin is not None and tmin <= config.FROST_T_MIN_C


def wx_as_dict(wx: WxLive) -> dict[str, Any]:
    """JSON-serialisierbare Metadaten (für Kontext-Blöcke, keine 24-Zeiten-Massen)."""
    tmin = wx_min_12h(wx)
    swr = wx_solar_12h(wx)
    return {
        "wx_quelle": wx.wx_quelle,
        "wx_run": wx.wx_run,
        "wx_age_h": None if wx.wx_age_h is None else round(wx.wx_age_h, 2),
        "wx_min_12h_c": None if tmin is None else round(tmin, 1),
        "wx_solar_12h_wh_m2": None if swr is None else round(swr, 0),
        "window_utc": [wx.utc_hour[0].isoformat(), wx.utc_hour[-1].isoformat()],
    }
