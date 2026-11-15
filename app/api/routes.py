"""FastAPI routes for the CMI gateway (Sprint S1: read-only + S3-1 change).

Endpoints:

* ``GET /``              -- Web-UI (static, S4; mounted in app.main, no token)
* ``GET /api/v1/state``  -- full normalized live state (bearer-protected)
* ``GET /api/v1/params`` -- all settable parameters + options (bearer-protected)
* ``GET /api/v1/history``-- persisted time-series query (bearer-protected, pure DB)
* ``GET /api/v1/health`` -- gateway + CMI + history reachability (open)
* ``GET /api/v1/recommend``-- L2 v0.1 recommendations (S4N-UI-KI; see
  ``app.api.recommend_route``)
* ``GET/POST /api/v1/change`` -- S3-1 two-phase write (see
  ``app.change_route``)
* ``/docs``              -- OpenAPI (open)

Authentication: ``Authorization: Bearer <token>``.  The token must equal
``GATEWAY_READ_TOKEN`` or ``GATEWAY_WRITE_TOKEN``.  When both env vars are
empty, auth is disabled (local development only -- never do that in
production!).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.cmi.cmi_log_pq import CmiLogPqError, CmiLogPqInputError, CmiLogPqStoreError
from app.config import Settings
from app.db import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_RANGE_HOURS,
    MAX_HISTORY_LIMIT,
    HistoryQuery,
    parse_ts,
)
from app.state import StateStore
from app.winsol_wide import (
    WINSOL_DEVICE_RE,
    WinsolInputError,
    WinsolStoreError,
    WinsolWideError,
)

logger = logging.getLogger("cmi.api")


class ParamOption(BaseModel):
    value: str
    label: str
    selected: bool = False


class ParamView(BaseModel):
    """One settable parameter (display address) with options and labels."""

    address: str
    x2: bool
    labels: list[str]
    locations: list[str]
    current_raw: str | None = None
    change_address: str | None = None
    option_type: str | None = None
    options: list[ParamOption] = Field(default_factory=list)
    settable_values: list[str] = Field(default_factory=list)
    resolved_at: float | None = None
    error: str | None = None


class HealthView(BaseModel):
    status: str  # ok | degraded | down
    gateway: dict
    cmi: dict
    stale: bool
    #: S2-1: history DB bookkeeping (last-insert ts, row count, error flag).
    history: dict
    #: S2-Parquet-Pivot: WinSol Parquet store availability (dir + month range).
    winsol: dict
    #: S4N-LogPoll: CMI /LOG poller status (error flag, last poll, last ingest).
    cmi_log: dict


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_bearer(request: Request) -> str:
    """Bearer-token dependency for the /api/v1/* read routes.

    Accepts either the read token or the write token.  If neither token is
    configured, the route is open (development mode).
    """
    settings: Settings = request.app.state.settings
    valid = {t for t in (settings.gateway_read_token, settings.gateway_write_token) if t}
    if not valid:
        return "open"
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="cmi-gateway"'},
        )
    token = header.removeprefix("Bearer ").strip()
    if token not in valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid Bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="cmi-gateway"'},
        )
    return "ok"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter()


@router.get("/api/v1/health", response_model=HealthView, summary="Gateway- + CMI-Health")
async def health(request: Request) -> JSONResponse:
    """Gateway + CMI reachability (open, no token required).

    Performs a live schematic ping at most every 30 s; between pings the last
    known route states are reported.
    """
    gateway = request.app.state.gateway
    now = time.time()

    # Throttled live CMI ping (schematic route is rate-unlimited, ~0.7 s).
    ping_result: str | None = None
    if now - getattr(gateway, "_last_health_ping", 0.0) > 30.0:
        gateway._last_health_ping = now  # type: ignore[attr-defined]
        try:
            html = await gateway.guard.run_schematic(
                lambda: gateway.client.get_schematic(1), timeout=10.0
            )
            ping_result = "ok" if html.strip() else "empty"
        except Exception as exc:  # noqa: BLE001 - any failure downgrades CMI health
            ping_result = f"error: {exc}"

    store: StateStore = gateway.store
    if ping_result == "ok":
        store.mark_schematic_ping(True, ts=now)
    elif ping_result is not None:
        store.mark_schematic_ping(False, error=ping_result, ts=now)

    store_snapshot = store.snapshot()
    health = store_snapshot.health
    cmi_reachable = health.schematic_ok or health.api_ok

    # S2-1: history DB bookkeeping (last-insert ts + row count + error flag).
    try:
        history_status = gateway.history_status()
    except Exception:  # noqa: BLE001 - health must not crash on a DB problem
        history_status = {"last_insert_ts": None, "rows": 0, "first_ts": None, "error": "unavailable"}

    # S2-Parquet-Pivot: WinSol Parquet store availability (cheap fs probe).
    try:
        winsol_status = gateway.winsol_status()
    except Exception:  # noqa: BLE001 - health must not crash on a store problem
        winsol_status = {"available": False, "dir": None, "months": None}

    # S4N-LogPoll: CMI /LOG poller status (latched error + last poll/ingest).
    try:
        cmi_log_status = gateway.cmi_log_status()
    except Exception:  # noqa: BLE001 - health must not crash on a poller problem
        cmi_log_status = {"error": "unavailable", "last_poll_ts": None,
                          "last_ingested_files": 0,
                          "store": {"available": False, "dir": None, "months": None}}

    body = HealthView(
        status="ok" if cmi_reachable else "down",
        gateway={"status": "running", "version": request.app.state.version},
        cmi={
            "url": gateway.settings.base_url,
            "schematic": {
                "ok": health.schematic_ok,
                "last_time": health.schematic_time,
                "last_error": health.schematic_error,
            },
            "json_api": {
                "ok": health.api_ok,
                "last_time": health.api_time,
                "last_error": health.api_error,
            },
            "stale": health.stale,
        },
        stale=health.stale,
        history=history_status,
        winsol=winsol_status,
        cmi_log=cmi_log_status,
    )
    status_code = 200 if cmi_reachable else 503
    return JSONResponse(status_code=status_code, content=body.model_dump())


@router.get(
    "/api/v1/state",
    summary="Vollstaendiger Live-State (Schematic + JSON-API-Fusion)",
    dependencies=[Depends(require_bearer)],
)
async def state(request: Request) -> dict:
    store: StateStore = request.app.state.gateway.store
    doc = store.snapshot()
    return doc.model_dump(mode="json")


@router.get(
    "/api/v1/params",
    response_model=list[ParamView],
    summary="Setzbare Parameter mit Options + Labels",
    dependencies=[Depends(require_bearer)],
)
async def params(request: Request) -> list[ParamView]:
    """All settable parameters found in the schematics, lazy-resolved.

    Display addresses are collected from the current state document; the
    changer resolver translates them (cached, TTL 24 h).  Addresses that
    cannot be resolved are returned with their ``error`` field set.
    """
    gateway = request.app.state.gateway
    store: StateStore = gateway.store
    doc = store.snapshot()

    # Collect settable addresses: address -> (x2, curated labels, locations, raws)
    collected: dict[str, tuple[bool, list[str], list[str], list[str]]] = {}
    for page in doc.schematics.values():
        for pos in page.positions:
            if not pos.address:
                continue
            entry = collected.setdefault(
                pos.address, (pos.x2, [], [f"page={page.page};pos={pos.index}"], [])
            )
            if pos.text:
                entry[3].append(pos.text)
            # The parser already resolved the label (proximity -> curated map),
            # so a value block carries a human label directly.
            if pos.label:
                entry[1].append(pos.label)

    views: list[ParamView] = []
    ok_count = 0
    for address, (x2, label_texts, locations, raws) in collected.items():
        view = ParamView(
            address=address,
            x2=x2,
            labels=sorted(set(label_texts)),
            locations=locations,
            current_raw=raws[0] if raws else None,
        )
        try:
            info = await gateway.changer.resolve(address, x2=x2)
            view.change_address = info.change_address
            view.option_type = info.option_type
            view.options = [
                ParamOption(value=o.value, label=o.label, selected=o.selected) for o in info.options
            ]
            view.settable_values = info.settable_values
            view.resolved_at = info.resolved_at
            ok_count += 1
        except Exception as exc:  # noqa: BLE001 - keep going, report per-address errors
            view.error = str(exc)
            logger.warning("params: cannot resolve %s: %s", address, exc)
        views.append(view)

    store.set_params_resolved(ok_count)
    return views


# ---------------------------------------------------------------------------
# History (S2-1): pure-DB range/series queries, no live CMI calls
# ---------------------------------------------------------------------------
_VALID_AGGS = {"last", "min", "max", "avg"}


def _parse_channels(raw: list[str] | None) -> tuple[int, ...]:
    """Parse the ``channel`` param (repeatable and/or comma-separated)."""
    if not raw:
        return ()
    channels: list[int] = []
    for chunk in raw:
        for part in str(chunk).split(","):
            part = part.strip()
            if part:
                channels.append(int(part))
    return tuple(dict.fromkeys(channels))  # de-dup, keep order


@router.get(
    "/api/v1/history",
    summary="Historie: time-series query on persisted poll samples",
    dependencies=[Depends(require_bearer)],
)
async def history(
    request: Request,
    start: Annotated[str | None, Query(description="ISO start (YYYY-MM-DD[ HH:MM:SS]), default now-24h")] = None,
    end: Annotated[str | None, Query(description="ISO end, default now")] = None,
    device: Annotated[str | None, Query(description="device filter, e.g. 'schematic' | 'json:32'")] = None,
    channel: Annotated[
        list[str] | None,
        Query(description="channel filter(s); repeatable (?channel=1&channel=2) and/or comma-sep (?channel=1,2)"),
    ] = None,
    metric: Annotated[
        str | None,
        Query(
            description=(
                "source filter for the LONG store ('schematic' | 'json'). "
                "Ignored for WinSol series (winsol:<N>), which are served from the "
                "canonical Parquet store, not the long store."
            )
        ),
    ] = None,
    ad: Annotated[
        str | None,
        Query(
            description=(
                "A/D type filter: 'A' (analog/measurement) or 'D' (digital/switch). "
                "Omitted (default) -> the union of both, the original contract. "
                "Applied to BOTH the long store (measurements.ad) and the WinSol "
                "Parquet store; e.g. ?device=winsol:1&channel=2&ad=A returns only "
                "the analog column, not the digital twin of the same channel."
            )
        ),
    ] = None,
    agg: Annotated[str, Query(description="last|min|max|avg (default last)")] = "last",
    interval: Annotated[int | None, Query(ge=1, description="bucket seconds for downsampled series")] = None,
    limit: Annotated[int, Query(ge=1)] = DEFAULT_HISTORY_LIMIT,
) -> JSONResponse:
    """Return persisted history for one or more device/channel series.

    Pure database read (no CMI calls).  Response shape:

    * a single device/channel -> ``{device, channel, points, count, agg, interval}``
    * multiple -> a dict keyed by ``"device:channel"``, each ``{points, count, ...}``,
      plus a ``_meta`` key with the total.

    ``points`` are ``[{"ts", "value"}]`` ascending.  With ``interval`` + a
    ``min/max/avg`` aggregation the series is downsampled into buckets; with
    ``last`` + ``interval`` each bucket holds the most recent sample.
    WinSol series (device ``winsol:<N>``) are served ON DEMAND from the
    canonical Parquet stores instead of the long store, which holds only
    live-polling rows: the read-only WinSol archive
    (:class:`app.winsol_wide.WinsolParquet`, ``winsol_pq``) PLUS the live
    cmi-log store (:class:`app.cmi.cmi_log_pq.CmiLogParquet`,
    ``cmi_log_pq``).  Per (device, channel, ad) the two stores are merged into
    one logical series: the ts-union, with the WinSol archive winning on
    timestamp collisions (Rosetta-canonical archive vs. the CMI's internal
    logger).  They are merged into the same response as the long-store series
    (one contract for the UI).  The ``metric`` filter is a long-store concept
    and is ignored for WinSol series.

    ``ad`` optionally selects the A/D type (``'A'`` analog / ``'D'`` digital)
    for BOTH stores; omitted means the union of both (original contract).
    """
    if agg not in _VALID_AGGS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid agg {agg!r}; expected one of {sorted(_VALID_AGGS)}",
        )
    if ad is not None and ad not in ("A", "D"):
        raise HTTPException(status_code=400, detail=f"invalid ad {ad!r}; expected 'A' or 'D'")
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    try:
        now = time.time()
        end_ts = parse_ts(end) if end else None
        start_ts = parse_ts(start) if start else None
        # Defaults: [now-24h, now].
        if end_ts is None:
            end_ts = _fmt_now(now)
        if start_ts is None:
            start_ts = _fmt_now(now - DEFAULT_RANGE_HOURS * 3600)
        if start_ts > end_ts:
            raise HTTPException(status_code=400, detail="start must be <= end")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    channels = _parse_channels(channel)
    if any(c < 0 for c in channels):
        raise HTTPException(status_code=400, detail="channel must be >= 0")

    gateway = request.app.state.gateway
    wide_keys, long_device = _split_winsol_query(gateway, device, channels, ad)
    long_q = HistoryQuery(
        start=start_ts,
        end=end_ts,
        device=long_device,
        channels=channels,
        metric=metric,
        ad=ad,
        agg=agg,
        interval=interval,
        limit=limit,
    )
    try:
        wide_map = (
            await asyncio.to_thread(
                _query_winsol_series, gateway, wide_keys, start_ts, end_ts, agg, interval, limit
            )
            if wide_keys is not None
            else {}
        )
        long_map = await asyncio.to_thread(gateway.history.query_series_map, long_q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (WinsolInputError, CmiLogPqInputError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (WinsolStoreError, CmiLogPqStoreError, sqlite3.Error) as exc:
        raise HTTPException(status_code=500, detail=f"history query failed: {exc}") from exc

    series = {**long_map, **wide_map}
    return JSONResponse(content=_shape_history_response(series, agg, interval))


def _is_winsol_device(device: str | None) -> bool:
    """True when the device filter targets a WinSol wide-store namespace."""
    return bool(device) and bool(WINSOL_DEVICE_RE.match(device))


def _device_wide_channels(wide, cmi_wide, device: str, ad: str | None = None) -> set[int]:
    """All channels that exist for a ``winsol:<N>`` device in EITHER wide store.

    Union of the winsol_pq archive and the cmi_log_pq live store (the logical
    series spans both; cmi-log only fills the channels it actually logs).  An
    unavailable store simply contributes nothing; input problems of one store
    do not mask the other.
    """
    found: set[int] = set()
    with contextlib.suppress(WinsolWideError):
        found |= wide.available_channels(device, ad)
    with contextlib.suppress(CmiLogPqError):
        found |= cmi_wide.available_channels(device, ad)
    return found


def _winsol_device_channels(wide, cmi_wide, device: str, channels: tuple[int, ...],
                            ad: str | None = None) -> set[int]:
    """The requested channels that exist for a ``winsol:<N>`` device in
    EITHER wide store (the logical series spans both).

    Channels without a matching wide column in both stores are dropped (the
    stores are the source of truth for what exists).  An empty ``channels``
    means "all channels of the device" (device filter, no channel filter).
    """
    if channels:
        return set(channels) & _device_wide_channels(wide, cmi_wide, device, ad)
    return _device_wide_channels(wide, cmi_wide, device, ad)


def _split_winsol_query(gateway, device: str | None, channels: tuple[int, ...],
                        ad: str | None = None):
    """Split a history request into its WinSol (wide-store) and long-store parts.

    Returns ``(wide_keys, long_device)``:

    * ``wide_keys`` -- ``[(device, channel, ad_or_None), ...]`` for the WinSol
      series to read from the wide stores; ``None`` when no winsol series is
      requested.  For a winsol ``device`` filter these are that device's
      channels (all when ``channels`` is empty), where "exist" means present in
      the winsol_pq archive OR the cmi_log_pq live store (the logical series
      spans both).  When NO device filter is given, each requested channel is
      checked against the three winsol devices the same way.  The A/D filter
      (``ad``) restricts which wide columns count as "existing" for a channel
      (``ad='A'`` on a digital-only channel -> no series, not an error).
    * ``long_device`` -- the ``device`` value to pass to the long store
      (unchanged; the long store holds no winsol rows so winsol devices simply
      yield no long-store series).
    """
    wide = gateway.winsol
    cmi_wide = gateway.cmi_log_pq
    wide_keys: list[tuple[str, int, int | None]] = []
    if _is_winsol_device(device):
        if channels:
            chs = sorted(_winsol_device_channels(wide, cmi_wide, device, channels, ad))
        else:
            try:
                chs = sorted(_winsol_device_channels(wide, cmi_wide, device, (), ad))
            except WinsolWideError:
                chs = []
        for ch in chs:
            wide_keys.append((device, ch, ad))
    elif device is None:
        if channels:
            for ch in channels:
                for n in (0, 1, 2):
                    d = f"winsol:{n}"
                    if ch in _winsol_device_channels(wide, cmi_wide, d, (ch,), ad):
                        wide_keys.append((d, ch, ad))
        else:
            # No filter at all: every channel of every winsol device that exists
            # in EITHER wide store (the logical series spans both).
            for n in (0, 1, 2):
                d = f"winsol:{n}"
                for ch in sorted(_winsol_device_channels(wide, cmi_wide, d, (), ad)):
                    wide_keys.append((d, ch, ad))
    return (wide_keys or None), device


def _merge_wide_points(a: list[dict], b: list[dict]) -> list[dict]:
    """Merge two wide-store ``[{"ts","value"}]`` series (ascending).

    Result is the ts-union, ascending; on a ts collision the FIRST series
    (``a`` = the winsol_pq Rosetta-canonical archive) wins.  Used by
    :func:`_query_winsol_series` to fold the cmi_log_pq live store into the
    winsol archive per (device, channel, ad) -- one logical series.
    """
    if not a:
        return list(b)
    if not b:
        return list(a)
    i = j = 0
    out: list[dict] = []
    while i < len(a) and j < len(b):
        ta, tb = a[i]["ts"], b[j]["ts"]
        if ta == tb:
            out.append(a[i])  # archive wins on collision
            i += 1
            j += 1
        elif ta < tb:
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def _query_winsol_series(gateway, wide_keys, start, end, agg, interval, limit):
    """Read every ``wide_key`` from BOTH wide stores and merge them
    (synchronous; caller threads it).

    Per (device, channel, ad): the winsol_pq archive points are folded with
    the cmi_log_pq live points via :func:`_merge_wide_points` (ts-union,
    archive wins on ts collisions) -> one logical series per wide key.
    A store that is not available simply contributes no points (clean
    degradation).  ``WinsolInputError`` / ``CmiLogPqInputError`` from one
    store mean "this channel is not in that store's columns" and are skipped
    for THAT store only -- the logical series is the union of both stores, so
    a channel present in just one of them must still be served (the route
    validated the union up front in :func:`_split_winsol_query`).  Store-level
    errors (``WinsolStoreError`` / ``CmiLogPqStoreError``) propagate to the
    route (mapped to 500).
    """
    wide = gateway.winsol
    cmi_wide = gateway.cmi_log_pq
    out: dict[str, list[dict]] = {}
    for device, ch, ad in wide_keys:
        points: list[dict] = []
        if wide.is_available():
            try:
                points = wide.query(
                    device, ch, ad, start, end, agg=agg, interval=interval, limit=limit
                )
            except WinsolInputError:
                points = []  # channel not in the archive's columns -> no points here
        if cmi_wide.is_available():
            try:
                cmi_pts = cmi_wide.query(
                    device, ch, ad, start, end, agg=agg, interval=interval, limit=limit
                )
            except CmiLogPqInputError:
                cmi_pts = []  # channel not in the live store's columns -> no points here
            else:
                points = _merge_wide_points(points, cmi_pts)
        if points:
            out[f"{device}:{ch}"] = points
    return out


def _shape_history_response(series: dict[str, list[dict]], agg: str, interval) -> dict:
    """Shape a ``{f"{device}:{channel}": points}`` map into the API response.

    Mirrors :meth:`app.db.HistoryDB.query_series`: a single series returns the
    flat shape, multiple return a dict keyed by ``"device:channel"`` plus a
    ``_meta`` total.  An empty map returns ``{}`` (unchanged from S2-1).
    """
    if not series:
        return {}
    if len(series) == 1:
        key, points = next(iter(series.items()))
        device, channel = key.rsplit(":", 1)
        return {
            "device": device,
            "channel": int(channel),
            "points": points,
            "count": len(points),
            "agg": agg,
            "interval": interval,
        }
    out: dict[str, dict] = {}
    total = 0
    for key, points in series.items():
        out[key] = {
            "points": points,
            "count": len(points),
            "agg": agg,
            "interval": interval,
        }
        total += len(points)
    out["_meta"] = {"count": total, "agg": agg, "interval": interval}
    return out


def _fmt_now(epoch: float) -> str:
    """Default-window timestamp: naive local wall-clock at ``epoch``."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
