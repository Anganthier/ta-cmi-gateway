"""L2 recommendation service: in-gateway engine runs + hour-keyed caching.

Wires the L2 v0.1 engine (``analysis.model.l2``) into the gateway process for
``GET /api/v1/recommend`` (S4N-UI-KI).  Design constraints
(``docs/ai-model-design.md`` §3.2 + ``docs/l2-ui-panel.md``):

* The engine is deterministic and offline: feature store m1.3 (read-only) +
  LIVE Open-Meteo ICON-D2 forecast + L1 v003 rollouts.  No CMI access.
* **Result caching**: one run per full hour, cached in memory with a TTL
  (``Settings.recommend_cache_min``, default 5 min).  The cache key is the
  current full hour (Europe/Berlin); a hit inside the TTL returns the last
  run without re-running the pipeline.  Cache violation (new hour or expiry)
  = one fresh engine run; concurrent callers for the same key share a single
  in-flight run (no double runs).
* **Stale semantics**: the engine itself computes ``stale=true`` (metric gap
  / forecast outage) and still returns a valid "beibehalten" recommendation
  -> the route answers HTTP 200 with ``stale=true`` (design §3.2 rule R5),
  never 5xx for staleness.
* **Error semantics**: hard engine failures (store missing/unreadable,
  broken artifact, unexpected crash) -> :class:`RecommendationError`, which
  the route maps to HTTP 503 with a JSON error body (no bare crash-500).  A
  failed run does NOT poison the cache: it is discarded from the in-flight
  table as soon as it is observable, so the next call (including a plain
  retry or ``force``) starts a fresh run.

The service is created per app instance (``app.state.recommendation``);
``aclose()`` cancels any in-flight engine task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings

logger = logging.getLogger("cmi.recommendation")

__all__ = [
    "EngineUnavailable",
    "RecommendationError",
    "RecommendationService",
    "berlin_hour_key",
]

#: Europe/Berlin (the plant's time zone).  Mirrors ``analysis.model.l2.
#: weather.BERLIN`` but is defined here so that importing this module NEVER
#: requires the ``analysis/`` tree to be importable: app startup, health and
#: every non-L2 route must work on a stale image / dev checkout where the
#: engine is absent (degradation = a clean 503 JSON from /api/v1/recommend).
BERLIN: ZoneInfo = ZoneInfo("Europe/Berlin")


class RecommendationError(Exception):
    """Hard L2 engine failure (route maps it to HTTP 503 with a JSON body)."""


class EngineUnavailable(RecommendationError):
    """The L2 engine cannot be imported/loaded (``analysis/`` missing, broken
    artifact).  The route reports it with the same 503 JSON contract."""


def berlin_hour_key(now: datetime | None = None) -> str:
    """Cache key: the current full hour in Europe/Berlin (``YYYY-MM-DD HH:00``).

    Always normalises to Berlin local time, whether ``now`` is naive, UTC-aware
    or already Berlin-aware: the cache key is a BERLIN hour, independent of the
    process time zone.
    """
    dt = (now or datetime.now(UTC)).astimezone(BERLIN)
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00")


class _Engine:
    """Lazy holder for the L2 engine + L1 predictor (one-time import/load).

    The engine is imported (not called) on first use so that a missing
    ``analysis/`` tree (old image / dev environment) degrades to a clean
    :class:`EngineUnavailable` instead of crashing app startup.
    """

    def __init__(self) -> None:
        self._predictor: Any | None = None

    def ensure(self, settings: Settings) -> None:
        """Import the engine; raise :class:`EngineUnavailable` when absent."""
        try:
            import analysis.model.l2  # noqa: F401 - availability check only
        except ImportError as exc:
            raise EngineUnavailable(
                f"L2-Engine (analysis/model/l2) im Image nicht importierbar: {exc}"
            ) from exc

    def predictor(self, settings: Settings) -> Any:
        """The L1 v003 predictor, loaded once per process (read-only artifact)."""
        if self._predictor is None:
            try:
                from analysis.model.m2b.predict import L1Predictor

                self._predictor = L1Predictor.load(settings.l1_model_dir)
            except Exception as exc:  # noqa: BLE001 - any load failure = 503
                raise EngineUnavailable(
                    f"L1-Artefakt {settings.l1_model_dir} nicht ladbar: {exc}"
                ) from exc
        return self._predictor


class RecommendationService:
    """Hour-keyed, in-memory cache around one L2 engine run.

    * key = :func:`berlin_hour_key` (full hour, Europe/Berlin);
    * entry = ``{"payload": <L2 JSON dict>, "generated_at": <UTC ISO>,
      "_at": <epoch>}``;
    * TTL = ``settings.recommend_cache_min`` minutes (default 5);
    * one ``asyncio.Lock`` serialises cache/in-flight bookkeeping; concurrent
      callers for the same hour share a single in-flight run (``Event``),
      so the engine executes at most once per key.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine = _Engine()
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # hour -> [event, error-or-None]; present while a run is in flight.
        # On success the entry is dropped (the cache entry wins); on failure
        # the error is stored in ``[1]`` and waiters observe it via get().
        self._inflight: dict[str, list[Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def cache(self) -> dict[str, dict[str, Any]]:
        """The raw cache (for tests / introspection)."""
        return self._cache

    def _ttl_sec(self) -> float:
        return max(0.0, float(self._settings.recommend_cache_min) * 60.0)

    # -- engine run (blocking, off the event loop) ---------------------------
    def _run_sync(self, hour: str) -> dict[str, Any]:
        """Blocking engine run for one hour (called off the event loop).

        Returns ``{"payload": <L2 JSON dict>, "generated_at": <UTC ISO>}``.
        Raises :class:`RecommendationError` on any hard failure.
        """
        self._engine.ensure(self._settings)
        predictor = self._engine.predictor(self._settings)
        from analysis.model.l2.recommend import recommend

        started = time.monotonic()
        try:
            rec = recommend(
                hour,
                features_dir=self._settings.l1_store_dir,
                artifact_dir=self._settings.l1_model_dir,
                predictor=predictor,
            )
        except RecommendationError:
            raise
        except Exception as exc:  # noqa: BLE001 - all engine errors -> 503
            raise RecommendationError(
                f"L2-Engine-Fehler (Stunde {hour!r}): {exc}"
            ) from exc
        payload = rec.to_json()
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        logger.info(
            "L2-Run für %s ok (stale=%s, %d Empfehlungen, %.2f s)",
            hour,
            payload.get("stale"),
            len(payload.get("empfehlungen") or []),
            time.monotonic() - started,
        )
        return {"payload": payload, "generated_at": payload["generated_at"]}

    # -- public API ------------------------------------------------------------
    async def get(self, force: bool = False) -> dict[str, Any]:
        """The recommendation payload for the current hour (cached, TTL-guarded).

        ``force=True`` (UI "Jetzt aktualisieren") discards the cached entry
        of the current hour and triggers a fresh engine run.  A still-pending
        in-flight run is always shared, never duplicated.  A run that FAILED
        earlier in this hour is retried automatically (failures never
        poison the cache).

        Raises:
            RecommendationError: hard engine failure (route -> HTTP 503 JSON).
        """
        hour = berlin_hour_key()
        ttl = self._ttl_sec()

        async with self._lock:
            entry = self._cache.get(hour)
            fresh = entry is not None and time.time() - entry["_at"] < ttl
            if not force and fresh:
                return entry["payload"]
            self._cache.pop(hour, None)  # expired or forced
            item = self._inflight.get(hour)
            if item is not None and item[1] is not None:
                # Previous run of this hour already failed -> drop it and
                # retry with a fresh run (no cache poisoning).
                self._inflight.pop(hour, None)
                item = None
            if item is None:
                event: asyncio.Event = asyncio.Event()
                self._inflight[hour] = [event, None]
                task = asyncio.create_task(
                    self._execute(hour, event), name=f"l2-run-{hour.replace(' ', '-')}"
                )
                self._tasks[hour] = task
            else:
                event = item[0]

        await event.wait()

        async with self._lock:
            entry = self._cache.get(hour)
            if entry is not None:
                return entry["payload"]
            item = self._inflight.get(hour)
            err = item[1] if item is not None else None
            if err is not None:
                raise err
            raise RecommendationError(f"L2-Ergebnis für {hour!r} fehlt")

    async def _execute(self, hour: str, event: asyncio.Event) -> None:
        """One engine run: success -> cache; failure -> in-flight error.

        Cancellation-safe: ``event.set()`` runs in ``finally`` so that a
        cancelled task (app shutdown via :meth:`aclose`) can NEVER leave a
        waiting :meth:`get` caller blocked forever.  A cancelled run does not
        write to the cache (no poisoning); the waiter observes the absence
        and raises.
        """
        cancelled = False
        try:
            entry = await asyncio.to_thread(self._run_sync, hour)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001 - any failure -> 503 JSON later
            err: Exception = (
                exc if isinstance(exc, RecommendationError) else RecommendationError(str(exc))
            )
            async with self._lock:
                item = self._inflight.get(hour)
                if item is not None:
                    item[1] = err
                self._tasks.pop(hour, None)
            logger.warning("L2-Run für %s fehlgeschlagen: %s", hour, err)
        else:
            async with self._lock:
                self._cache[hour] = {**entry, "_at": time.time()}
                self._inflight.pop(hour, None)
                self._tasks.pop(hour, None)
        finally:
            if cancelled:
                # Drop any in-flight bookkeeping so a later call starts fresh.
                async with self._lock:
                    self._inflight.pop(hour, None)
                    self._tasks.pop(hour, None)
            event.set()

    def invalidate(self, hour: str | None = None) -> None:
        """Drop one (or all) cache entries (tests / config reloads)."""
        if hour is None:
            self._cache.clear()
        else:
            self._cache.pop(hour, None)

    async def aclose(self) -> None:
        """Cancel any in-flight engine task (app shutdown)."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
