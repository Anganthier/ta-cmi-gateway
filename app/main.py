"""Gateway entry point: FastAPI app + background pollers.

Run locally::

    uvicorn app.main:app --host 0.0.0.0 --port 30001

or ``python -m app.main`` (uses GATEWAY_HOST/GATEWAY_PORT env vars).

Architecture (Sprint S1, read-only)::

    /api/v1/*  -->  routes (Bearer auth)  -->  StateStore  <--  Gateway pollers
                                                            |
                              CMIClient (httpx, Basic-Auth) <--+
                              CMIGuard (serial queue + 1/min rate limiter)
                              ChangerResolver (TTL + file cache)
                                   |
                                   v
                          TA CMI @ CMI_BASE_URL (read-only endpoints)

Web-UI (Sprint S4/S4N, read-only, loop-zentrisch)::

    /          -->  ui/index.html + static ui/ (css/js/svg/vendor), mounted
                   LAST so /api/v1/* keeps full priority
    /assets/   -->  static assets/ (schematic {N}.html layouts used for the
                   loop Readout-Koordinaten; legacy {N}.png still served)

The UI is a vanilla-JS SPA with no backend of its own: all data comes from
the /api/v1/* endpoints with the read token. Since S4N-2 the UI renders 5
animated P&ID loop views (A-E) + a static "Gesamt" doc view instead of the
former 5 PNG schematic tabs. No CMI write paths are exposed or touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.recommend_route import router as recommend_router
from app.api.routes import router
from app.change_route import ChangeService
from app.change_route import router as change_router
from app.cmi.changer_resolver import ChangerResolver
from app.cmi.client import CMIClient, build_client
from app.cmi.cmi_log import (
    CLEAR_PATH,
    CMILogClient,
    CmiLogData,
    fetch_cmi_log_files,
    fetch_log_index,
)
from app.cmi.cmi_log_pq import CmiLogParquet, CmiLogPqError
from app.cmi.exceptions import (
    CMIAuthError,
    CMIConnectionError,
    CMIGatewayError,
    CMIJsonApiError,
    CmiLogError,
    CMIRateLimitedError,
    CMISchematicError,
)
from app.cmi.json_api import JsonApiClient
from app.cmi.queue import CMIGuard
from app.cmi.schematic_parser import SCHEMATIC_PAGES, load_schematic
from app.config import REPO_ROOT, Settings, get_settings
from app.db import get_history_db, json_rows, now_iso, schematic_rows
from app.recommendation import RecommendationService
from app.state import StateStore
from app.winsol_wide import WinsolParquet

logger = logging.getLogger("cmi.gateway")

#: Polling parameters for the JSON-API snapshots (node, jsonparam).
#: Node 1 (UVR1611): I,O,D -- D simply yields no DL-Bus section on non-x2
#: devices (verified live 2026-10-27: Inputs=16, Outputs=13).
API_SNAPSHOTS: tuple[tuple[int, str], ...] = (
    (1, "I,O,D"),
    (32, "I,O,D,La,Ld"),
)

SCHEMATIC_PAGES_TO_POLL = tuple(SCHEMATIC_PAGES)


class Gateway:
    """Wires client, guard, store, resolver and runs the pollers."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: CMIClient | None = None,
        store: StateStore | None = None,
        guard: CMIGuard | None = None,
        changer: ChangerResolver | None = None,
        api_start_delay: float = 3.0,
        cmi_log: CMILogClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or build_client(settings)
        self.guard = guard or CMIGuard(api_min_interval_sec=settings.cmi_api_interval_sec)
        self.store = store or StateStore(cmi_url=settings.base_url)
        self.changer = changer or ChangerResolver(
            self.client,
            self.guard,
            ttl_sec=settings.cmi_changer_ttl_sec,
            cache_dir=settings.cmi_data_dir / "changer_cache",
        )
        self.json_api = JsonApiClient(self.client, self.guard)
        # S4N-LogPoll: dedicated client for the CMI /LOG store (rate-unlimited,
        # NOT the serial CMIGuard JSON-API queue).
        self.cmi_log = cmi_log or CMILogClient(
            settings.base_url,
            settings.cmi_user,
            settings.cmi_pass,
            timeout_sec=settings.cmi_timeout_sec,
        )
        # S2-1: persistent live-poll history (separate from WinSol).
        self.history = get_history_db(settings.history_db_path)
        self.history.init_schema()
        # S2-Parquet-Pivot: WinSol history is served ON DEMAND from the canonical
        # Parquet store (read-only).  A missing/empty dir degrades cleanly
        # (no winsol series) and never fails gateway startup.
        self.winsol = WinsolParquet(settings.winsol_parquet_dir)
        # S4N-LogParquet: the CMI internal logging lives in its OWN wide
        # Parquet store (data/cmi_log_pq, same 71-column schema as winsol_pq);
        # the poller rewrites the in-progress month every 5 min atomically.
        # The history route merges it into the logical winsol:<N> series
        # (winsol_pq wins on ts collisions).
        self.cmi_log_pq = CmiLogParquet(settings.cmi_log_parquet_dir)
        self._history_error: str | None = None
        self._last_retention = 0.0
        self._tasks: list[asyncio.Task] = []
        self._last_health_ping = 0.0
        self.schematic_dir = settings.cmi_schematic_dir
        self._api_start_delay = api_start_delay
        # S4N-LogPoll: latched /LOG error flag (health surfacing, DoD 2c).
        self._cmi_log_error: str | None = None
        #: Last successful poller cycle timestamp (epoch seconds); 0.0 = none
        #: yet.  Refreshed at the end of EVERY successful cycle -- including
        #: an empty index (nothing new to ingest) -- so /health's
        #: last_poll_ts tracks the polling cadence, not just data arrival.
        self._cmi_log_last_poll: float = 0.0
        #: Data files ingested in the last cycle (health visibility).
        self._cmi_log_last_ingested: int = 0
        #: A Parquet persist failure in the CURRENT cycle (S4N-LogParquet).
        #: When set, the successful-clear of :attr:`_cmi_log_error` at the end
        #: of the cycle is withheld so a failed month rewrite stays visible on
        #: /health (and the not-yet-marked file is retried).  Reset every cycle
        #: so a *stale* latch from a clear failure does not survive the next
        #: (clean) cycle.
        self._cmi_log_persist_failed: bool = False
        #: Q-125 R4 (Decode-Blindheit): the CURRENT cycle fully downloaded
        #: new index files but decoded ZERO of them (each was rejected by the
        #: decoder, e.g. a /LOG binary format change: all new files fail the
        #: magic/record checks and are swallowed per-file).  When set, the
        #: cycle-end error-clear is withheld so a frozen store stays visible
        #: on /health until a cycle actually decodes >=1 file.  Total
        #: TRANSPORT failures (no download lands on disk) never set it.
        #: Reset every cycle, like :attr:`_cmi_log_persist_failed`.
        self._cmi_log_no_decode: bool = False

    # -- lifecycle ------------------------------------------------------------
    def start_pollers(self) -> None:
        """Start the background poller tasks (idempotent)."""
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._schematic_poller(), name="cmi-schematic-poller"),
            asyncio.create_task(self._api_poller(), name="cmi-json-api-poller"),
            asyncio.create_task(self._cmi_log_poller(), name="cmi-log-poller"),
        ]
        logger.info(
            "Pollers started: schematics every %.0fs, JSON-API snapshots every %.0fs, "
            "CMI /LOG store every %.0fs",
            self.settings.cmi_poll_sec,
            self.settings.cmi_api_interval_sec,
            self.settings.cmi_log_poll_sec,
        )

    async def stop_pollers(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def aclose(self) -> None:
        await self.stop_pollers()
        await self.client.aclose()
        await self.cmi_log.aclose()
        self.history.close()
        self.winsol.close()
        self.cmi_log_pq.close()

    # -- history persistence (S2-1) ------------------------------------------
    def history_status(self) -> dict:
        """Last-insert ts + row count + the history error flag (for /health)."""
        try:
            stat = self.history.stats()
        except sqlite3.Error as exc:  # pragma: no cover - only on a corrupt DB
            stat = {"rows": 0, "first_ts": None, "last_ts": None}
            self._history_error = f"read: {exc}"
        return {
            "last_insert_ts": stat["last_ts"],
            "rows": stat["rows"],
            "first_ts": stat["first_ts"],
            "error": self._history_error,
        }

    def winsol_status(self) -> dict:
        """Light WinSol Parquet availability block for /health.

        Derived from the filesystem layout (``YYYY/MM`` dirs) -- NOT a store
        scan -- so it is cheap even when the store is large.
        """
        return {
            "available": self.winsol.is_available(),
            "dir": str(self.winsol._path),
            "months": self.winsol.months_range(),
        }

    def cmi_log_status(self) -> dict:
        """Light CMI /LOG poller block for /health (S4N-LogPoll + S4N-LogParquet).

        Reports the latched error flag, the last successful poll timestamp and
        how many files the last cycle ingested, plus the cmi-log Parquet store
        availability.  Cheap: no CMI/DB access.
        """
        return {
            "error": self._cmi_log_error,
            "last_poll_ts": (
                datetime.fromtimestamp(self._cmi_log_last_poll).astimezone().isoformat()
                if self._cmi_log_last_poll
                else None
            ),
            "last_ingested_files": self._cmi_log_last_ingested,
            "store": {
                "available": self.cmi_log_pq.is_available(),
                "dir": str(self.cmi_log_pq._path),
                "months": self.cmi_log_pq.months_range(),
            },
        }

    def _maybe_retention(self) -> None:
        """Prune measurements older than the retention window (hourly).

        Runs off the event loop via :func:`asyncio.to_thread` is *not* used
        here because the call is short; it is invoked from the pollers, which
        are already background tasks.
        """
        if not self.settings.history_retention_days > 0:
            return
        if time.time() - self._last_retention < self.settings.history_retention_check_sec:
            return
        self._last_retention = time.time()
        try:
            # DECISION (human-approved): prune only live-poll sources
            # (HISTORY_PRUNE_SOURCES); historical sources stay forever.
            deleted = self.history.retention_cleanup(
                self.settings.history_retention_days,
                prune_sources=self.settings.prune_source_set,
            )
            if deleted:
                logger.info("History retention: pruned %d old measurements", deleted)
        except sqlite3.Error as exc:
            self._history_error = f"retention: {exc}"
            logger.warning("History retention failed: %s", exc)

    def _persist_schematic(self, parsed, ts: str) -> None:
        """Best-effort persistence of one parsed schematic page.

        Never raises: a DB write error is logged and latched on the history
        error flag so the poller survives and /health reports it.
        """
        try:
            rows = schematic_rows(parsed, ts)
            if rows:
                self.history.upsert(rows, source_name="schematic")
                self._history_error = None
        except sqlite3.Error as exc:
            self._history_error = f"schematic: {exc}"
            logger.warning("History persist (schematic) failed: %s", exc)

    def _persist_json(self, result, ts: str) -> None:
        """Best-effort persistence of one JSON-API node snapshot (see above)."""
        try:
            rows = json_rows(result, ts)
            if rows:
                self.history.upsert(rows, source_name=f"json:{result.node}")
                self._history_error = None
        except sqlite3.Error as exc:
            self._history_error = f"json:{result.node}: {exc}"
            logger.warning("History persist (json node %d) failed: %s", result.node, exc)

    # -- CMI /LOG poller (S4N-LogPoll) ----------------------------------------
    async def _cmi_log_cycle(self) -> None:
        """One best-effort /LOG ingestion cycle against the current year."""
        await self._cmi_log_cycle_for_year(datetime.now().year)

    async def _cmi_log_cycle_for_year(self, year: int) -> None:
        """One best-effort READ-ONLY /LOG ingestion cycle (index → new files).

        0. RAM-buffer flush: ``GET /LOG/infoh.log`` (WinSol semantics) before
           the index read; best-effort, NOT latching; without the flush the
           poller stays flush-limited (only flushed data visible).  Proven
           2026-10-31 (CIR-Log 09:22–09:38): the CMI keeps the internal /LOG
           logging in a RAM buffer and flushes it to disk (active data file +
           index size) ONLY on this read (the WinSol "Logger auslesen"
           first-read) -- data-GETs and index reads do not trigger a flush,
           so without it the poller sees only what a manual WinSol fetch has
           flushed (2026-10-31: 51-min data gap until the next WinSol click).
        1. Read the year index ``/LOG/info<JJJJ>.log`` (``year``).  On a 404
           (year change: no index for that year yet) the previous year's index
           is read instead and its still-buffered data files are ingested.
        2. Download + decode every data file whose basename is not yet in the
           ``cmi_log_ingested`` side table, OR whose stored size differs from
           the current index size (Name+SIZE dedup, S4N-LogPoll-DedupFix:
           the active block grows while listed in the index); rewrite the
           wide month
           file in the cmi-log Parquet store (``cmi_log_pq``,
           S4N-LogParquet) and mark each file (name + size + SHA-256).
        3. Buffer release is NOT done by this poller by default:
           ``settings.cmi_log_clear=False`` (the default) means the
           state-mutating ``GET /LOG/clear.log`` is NEVER sent.  A clear
           deletes every block in the CMI buffer -- INCLUDING the still-active
           block file that is not listed in the index yet -- so a
           poller-issued clear destroys not-yet-ingested data (proven
           2026-10-30 15:16:48: the active 00:00:05-15:16:48 block, ~5400
           records, was wiped without ever being ingested).  Buffer release is
           a manual WinSol-Fetch-with-Delete decision instead (user
           responsibility, documented in docs/cmi-log-protocol.md).
           ``settings.cmi_log_clear=True`` (experimental, legacy): the
           last-data-first gate is kept -- clear only when EVERY index file
           is already PERSISTED (marked in the cmi_log_ingested side table,
           which happens only after a successful Parquet write); across the
           year-boundary fallback the buffer state is unverifiable -> no clear
           in that case either.

        The CMI never deletes on its own: when the SD buffer fills up it
        simply stops logging (visible as a ts gap / break in the cmi-log
        Parquet store).  Reaching this cycle's end without an exception --
        including an EMPTY index (no files to ingest) -- clears the latched
        error and refreshes ``_cmi_log_last_poll`` so /health's
        ``last_poll_ts`` tracks the actual polling cadence, not the last
        time new data appeared.  A Parquet persist failure this cycle
        (``_cmi_log_persist_failed``) latches the error AND withholds the
        ``_cmi_log_last_poll`` refresh, so a failed month rewrite stays
        visible on /health until the next successful cycle; the
        not-yet-marked file is retried.  ``_cmi_log_last_ingested`` keeps its
        meaning: number of data files ingested in this cycle.  The same
        withholding applies to the Q-125 R4 *decode blind-spot* latch
        (``_cmi_log_no_decode``): if this cycle fully downloaded new index
        files but decoded NONE of them (e.g. a /LOG binary format change
        where every new file fails the decoder and is swallowed per-file by
        :func:`app.cmi.cmi_log.fetch_cmi_log_files`), the store is frozen --
        a healthy-looking cycle with a fresh ``last_poll_ts`` would hide that
        for days.  The cycle instead latches ``_cmi_log_error`` and keeps
        ``_cmi_log_last_poll`` stale so /health shows the blind spot until a
        later cycle actually decodes >=1 file; a PARTIAL failure (>=1 file
        decoded) never sets the flag and keeps today's per-file swallow
        behaviour.

        Never raises: every failure is logged + latched on
        :attr:`_cmi_log_error` so the poller survives and /health reports it.
        """
        fallback_year = False
        self._cmi_log_persist_failed = False
        self._cmi_log_no_decode = False
        try:
            # 0) Best-effort RAM-buffer flush (S4N-LogFlush, proven
            # 2026-10-31): the CMI keeps the internal /LOG logging in a RAM
            # buffer and flushes it to disk (active data file + index size)
            # ONLY when /LOG/infoh.log is read (the WinSol "Logger
            # auslesen" first-read) -- data-GETs and index reads do NOT
            # trigger a flush.  Without this read the poller stays
            # flush-limited: it only sees what a manual WinSol fetch has
            # flushed.  Read-only, ~6 kB, rate-unlimited; NOT part of the
            # ingestion guarantee: any failure is logged and swallowed
            # (never latched on _cmi_log_error, never aborts the cycle).
            try:
                await self.cmi_log.get_bytes("/LOG/infoh.log")
            except (CmiLogError, CMIConnectionError, CMIAuthError, OSError) as exc:
                logger.warning("cmi-log: flush-read infoh.log failed: %s", exc)

            try:
                files = await fetch_log_index(self.cmi_log, year)
            except (CmiLogError, CMIConnectionError) as exc:
                if getattr(exc, "http_status", None) != 404:
                    raise
                # 404: the current-year index does not exist yet (year
                # change).  The storekrit buffer may still hold the previous
                # year's data files -- ingest them best-effort.
                try:
                    files = await fetch_log_index(self.cmi_log, year - 1)
                    fallback_year = True
                except CMIAuthError:
                    raise
                except (CmiLogError, CMIConnectionError):
                    files = []  # no previous-year index either -> nothing to do
            # Empty index (or no previous-year index): nothing to ingest, but
            # the cycle itself succeeded -- clear the latched error and
            # refresh the poll ts so /health shows the poller is alive.
            if not files:
                self._cmi_log_error = None
                self._cmi_log_last_poll = time.time()
                return

            # Name+SIZE dedup (S4N-LogPoll-DedupFix): the active block grows
            # while it is listed in the index, so an index entry whose stored
            # size no longer matches is re-fetched (the ts-dedup in the
            # Parquet store keeps the re-ingestion idempotent).
            ingested = self.history.cmi_log_ingested_meta()
            decoded, now_ingested = await fetch_cmi_log_files(
                self.cmi_log,
                self.settings.cmi_data_dir / "cmi_log",
                files,
                ingested=ingested,
            )
            for data in decoded:
                self._persist_cmi_log(data)

            # Q-125 R4 (Decode-Blindheit): a TOTAL decode failure must not
            # look like a successful cycle.  fetch_cmi_log_files swallows a
            # per-file decode ValueError by design (S4N-LogPoll: one bad file
            # must not block the others), so on a /LOG binary format change
            # (e.g. 2026-11-07 16:14: P=181 records vs the P=169 decoder)
            # every new index file is undecodable and the store freezes while
            # /health would still report a fresh last_poll_ts.  Latch the
            # blind-spot flag when new files were fetched but NOT ONE of them
            # decoded; the cycle-end clear below then withholds BOTH the error
            # clear and the last_poll refresh until a cycle decodes >=1 file.
            # A PARTIAL failure (>=1 decoded) never latches: the swallow
            # behaviour for the individual bad file is unchanged (DoD 2b/2d).
            #
            # The on-disk evidence separates the two "0 decoded" causes
            # without touching the fetch/decode internals: a new file present
            # on disk at exactly the index size but absent from ``decoded``
            # was downloaded and REJECTED by the decoder (the blind spot ->
            # latch).  If all downloads failed instead, no new file lands on
            # disk and the cycle stays a swallow (today's
            # per-file transport behaviour, DoD 2d -- total 500/timeout).
            new_in_index = sum(1 for f in files if ingested.get(f.name) != f.size)
            if new_in_index > 0 and not decoded:
                data_dir = self.settings.cmi_data_dir / "cmi_log"
                # A new/grown file that (a) was NOT ingested this cycle and
                # (b) is present on disk at EXACTLY the index size was fully
                # downloaded this cycle (download_data_file only renames when
                # the streamed size matches the index size) yet rejected by
                # the decoder -> the blind spot.  A download that FAILED never
                # lands a fresh file on disk (or only a stale, SMALLER copy of
                # a growing block), so total transport failures stay a swallow
                # (today's per-file behaviour, DoD 2d -- total 500/timeout).
                undecodable = [
                    f.name
                    for f in files
                    if ingested.get(f.name) != f.size
                    and (data_dir / f.name).is_file()
                    and (data_dir / f.name).stat().st_size == f.size
                ]
                if undecodable:
                    self._cmi_log_no_decode = True
                    self._cmi_log_error = (
                        f"cmi-log: {new_in_index} new file(s) in index but 0 decoded "
                        f"- /LOG format change or corrupt files? "
                        f"(undecodable: {', '.join(undecodable)})"
                    )
                    logger.error("CMI /LOG decode blind spot: %s", self._cmi_log_error)

            # EXPERIMENTAL (cmi_log_clear=True only): last-data-first clear
            # gate -- release the CMI's storekrit buffer only when EVERY
            # index file is already PERSISTED at the EXACT index size (marked
            # in the cmi_log_ingested side table, which happens only after a
            # successful Parquet write; DoD 3 / Clear-Strategie A).  The gate
            # re-reads the side table after this cycle's persists and checks
            # ``meta[name] == f.size`` for every index file -- not just name
            # presence -- so (a) a file whose Parquet write failed can never
            # be cleared un-persisted, and (b) a still-growing active block
            # (stored size < index size) blocks the clear until it is
            # ingested at the current index size (S4N-LogPoll-DedupFix).
            # Across the year-boundary fallback the buffer state is
            # unverifiable -> no clear (the files are ingested; they leave
            # the buffer as soon as the CMI rolls over to the new year).
            # Default (False): the gate is skipped entirely and no
            # /LOG/clear.log is ever sent.
            meta = self.history.cmi_log_ingested_meta()
            if (
                self.settings.cmi_log_clear
                and not fallback_year
                and all(f.name in meta and meta[f.name] == f.size for f in files)
            ):
                body = await self.cmi_log.get_bytes(CLEAR_PATH)
                if not body.lstrip().upper().startswith(b"OK"):
                    raise CmiLogError(
                        f"CMI /LOG/clear.log answered {body[:32]!r} (want 'OK')"
                    )
                logger.info(
                    "cmi-log: /LOG/clear.log issued (all %d index files ingested, "
                    "%d new this cycle)", len(files), len(decoded),
                )
            # Reaching here without an exception means the index read and the
            # persist of every new file (and, with cmi_log_clear=True, a
            # successful /LOG/clear.log if the gate was open) all succeeded
            # this cycle.  Clear the latched error and refresh the poll ts --
            # UNLESS a persist failure latched one this cycle (a failed month
            # rewrite stays visible on /health until the next successful
            # cycle; the not-yet-marked file is retried and, with
            # cmi_log_clear=True, the clear gate above stayed closed) OR the
            # Q-125 R4 decode blind-spot latch is set (new files were fetched
            # but none decoded: a frozen store must stay visible on /health
            # until a cycle actually decodes >=1 file).
            if not self._cmi_log_persist_failed and not self._cmi_log_no_decode:
                self._cmi_log_error = None
                self._cmi_log_last_poll = time.time()
            self._cmi_log_last_ingested = len(decoded)
            if decoded:
                logger.info("cmi-log: ingested %d data file(s), %d new rows",
                            len(decoded), sum(len(d.rows) for d in decoded))
        except CmiLogError as exc:
            self._cmi_log_error = f"cmi-log: {exc}"
            logger.warning("CMI /LOG cycle failed: %s", exc)
        except (CMIConnectionError, CMIAuthError, TimeoutError) as exc:
            self._cmi_log_error = f"cmi-log: {exc}"
            logger.warning("CMI /LOG unreachable: %s", exc)
        except Exception as exc:  # noqa: BLE001 - poller must survive anything
            self._cmi_log_error = f"cmi-log: unexpected error: {exc}"
            logger.exception("CMI /LOG poller: unexpected error")

    async def _cmi_log_poller(self) -> None:
        """Poll the CMI /LOG store every ``cmi_log_poll_sec`` (S4N-LogPoll).

        READ-ONLY by default (``CMI_LOG_CLEAR=false``): the poller never sends
        the state-mutating ``/LOG/clear.log``, so the CMI buffer is never
        emptied by the gateway.  The CMI never deletes on its own either; when
        the SD buffer fills up it simply stops logging (visible as a ts break
        in the cmi-log Parquet store).  Buffer release is a manual
        WinSol-Fetch-with-Delete decision (user responsibility; see
        docs/cmi-log-protocol.md).
        """
        while True:
            await self._cmi_log_cycle()
            await asyncio.sleep(max(1.0, float(self.settings.cmi_log_poll_sec)))

    @staticmethod
    def _cmi_log_wide_records(data: CmiLogData) -> list[tuple[str, dict[str, float | None]]]:
        """Wide month-rewrite records for one decoded CMI /LOG data file.

        Pivots the long ``(ts, device, channel, ad, ...)`` rows into one wide
        row (71-column schema, cmi-log columns only, everything else NULL) per
        timestamp -- 70 rows per record become 1 wide row (S4N-LogFull: the
        decoder now emits all 70 value channels, so the wide row is fully
        populated).  The row order is
        the ts order of the source rows (ascending; the decoder emits them in
        wall-clock order).
        """
        wide: dict[str, dict[str, float | None]] = {}
        for ts, device, channel, ad, value, _unit, _source in data.rows:
            row = wide.setdefault(ts, {})
            col = f"dev{device.rsplit(':', 1)[1]}_{'analog' if ad == 'A' else 'digital'}_{channel:02d}"
            row[col] = value
        return [(ts, row) for ts, row in wide.items()]

    def _persist_cmi_log(self, data: CmiLogData) -> None:
        """Best-effort persistence of one decoded CMI /LOG data file.

        S4N-LogParquet: the rows are NOT written to ``history.db`` any more
        (that path held them under ``source='cmi-log'`` in S4N-LogPoll and was
        migrated out by ``analysis/cmi_log_backfill.py``).  Instead the wide
        month file ``cmi_log_pq/YYYY/MM/YYYY-MM.parquet`` is rewritten
        atomically (ts-de-duplicated, finished months untouched).  The file is
        marked in the ``cmi_log_ingested`` side table ONLY after a successful
        Parquet write -- and ``cmi_log_mark`` is an ``INSERT OR REPLACE``
        upsert, so re-ingesting a grown active block overwrites the stored
        size with the fresh index size (S4N-LogPoll-DedupFix) -- so a failed
        write is retried next cycle and, when the experimental clear gate is
        active (``cmi_log_clear=True``), stays safe
        (last-data-first: no file is ever cleared un-persisted).  A write
        error is logged + latched on :attr:`_cmi_log_error`.
        """
        try:
            records = self._cmi_log_wide_records(data)
            month = data.date[:7]  # YYYY-MM (the logged day's month)
            added = self.cmi_log_pq.write_month(month, records)
            self.history.cmi_log_mark(data.name, data.size, data.sha256, now_iso())
            logger.debug("cmi-log pq: %s -> month %s, %d new rows (%d records)",
                         data.name, month, added, data.n_decoded)
        except (CmiLogPqError, OSError) as exc:
            # A failed month rewrite latches the error AND flags the cycle so
            # the successful-clear at the end of the cycle is withheld (the
            # error stays visible on /health; the not-yet-marked file is
            # retried and the clear gate stays closed).  The latch is cleared
            # by the NEXT clean cycle, not here.
            self._cmi_log_error = f"cmi-log: {data.name}: {exc}"
            self._cmi_log_persist_failed = True
            logger.warning("CMI-Log Parquet persist (%s) failed: %s", data.name, exc)

    async def __aenter__(self) -> Gateway:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- pollers --------------------------------------------------------------
    async def _schematic_poller(self) -> None:
        """Poll all five schematic pages; survive any single failure."""
        while True:
            for page in SCHEMATIC_PAGES_TO_POLL:
                try:
                    html = await self.guard.run_schematic(
                        lambda p=page: self.client.get_schematic(p),
                        timeout=max(10.0, self.settings.cmi_timeout_sec),
                    )
                    parsed = load_schematic(self.schematic_dir, page, html)
                    self.store.update_schematic(parsed)
                    # S2-1: persist the sample (best-effort, never raises).
                    self._persist_schematic(parsed, now_iso())
                    self._maybe_retention()
                except CMISchematicError as exc:
                    logger.warning("Schematic page %d failed: %s", page, exc)
                    self.store.update_schematic_failed(str(exc))
                except (CMIConnectionError, CMIAuthError, CMIGatewayError,
                        TimeoutError) as exc:
                    logger.warning("Schematic page %d unreachable: %s", page, exc)
                    self.store.update_schematic_failed(str(exc))
                except Exception as exc:  # noqa: BLE001 - poller must survive anything
                    logger.exception("Schematic poller: unexpected error on page %d", page)
                    # Still surface the failure on the health route so the
                    # gateway does not report a healthy schematic route after
                    # an unexpected (non-CMI) error.
                    self.store.update_schematic_failed(f"unexpected error: {exc}")
            # Sleep the remainder of the poll period (requests took a few seconds).
            await asyncio.sleep(max(1.0, self.settings.cmi_poll_sec))

    async def _api_poller(self) -> None:
        """Poll the JSON-API snapshots; the rate limiter paces the loop.

        Node 1 (UVR1611): I,O,D -- no designations (non-x2).
        Node 32 (UVR610, x2): I,O,D,La,Ld + jsondesignation=1.
        """
        # Small delay so the schematic route can warm up first.
        await asyncio.sleep(self._api_start_delay)
        while True:
            for node, params in API_SNAPSHOTS:
                try:
                    result = await self.json_api.fetch(
                        node, params, designation=(node == 32)
                    )
                    self.store.update_api(result)
                    # S2-1: persist the sample (best-effort, never raises).
                    self._persist_json(result, now_iso())
                    self._maybe_retention()
                    logger.debug("JSON-API node %d (%s) ok: %d values",
                                 node, params, sum(len(v) for v in result.values.values()))
                except CMIRateLimitedError as exc:
                    logger.warning("JSON-API node %d rate-limited: %s (backing off %.0fs)",
                                   node, exc, self.settings.cmi_api_interval_sec)
                    self.store.update_api_failed(node, str(exc))
                    await asyncio.sleep(self.settings.cmi_api_interval_sec)
                except (CMIJsonApiError, CMIConnectionError, CMIGatewayError,
                        TimeoutError) as exc:
                    logger.warning("JSON-API node %d failed: %s", node, exc)
                    self.store.update_api_failed(node, str(exc))
                    await asyncio.sleep(30.0)
                except Exception as exc:  # noqa: BLE001 - poller must survive anything
                    logger.exception("JSON-API poller: unexpected error on node %d", node)
                    # Surface the failure on the health route even for
                    # unexpected (non-CMI) errors, then keep the loop running.
                    self.store.update_api_failed(node, f"unexpected error: {exc}")
                    await asyncio.sleep(30.0)
            # Loop pacing: each fetch already waited >= CMI_API_INTERVAL_SEC via
            # the limiter; no extra sleep needed.


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (pollers start via the lifespan hook).

    Static mounts (S4, Web-UI) are added LAST so the ``/api/v1/*`` router
    keeps full priority:

    * ``/assets`` -- schematic {N}.html layouts (loop Readout coordinates) +
                     legacy {N}.png (read-only, no auth)
    * ``/``       -- the UI (``ui/``, S4N loop views), served last;
                     ``index.html`` at the root
    """
    cfg = settings or get_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    app = FastAPI(
        title="CMI Gateway",
        version=__version__,
        description="Read-only gateway in front of the TA CMI heating control "
        "(Sprint S1: state + params + health).",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = cfg
    app.state.version = __version__
    app.state.gateway = Gateway(cfg)
    # S4N-UI-KI: L2 recommendation service (in-memory hour cache + engine).
    app.state.recommendation = RecommendationService(cfg)
    # S3-1: two-phase change route (write client + audit JSONL).
    app.state.change = ChangeService(cfg)

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.gateway.start_pollers()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.gateway.aclose()
        await app.state.recommendation.aclose()
        await app.state.change.aclose()

    app.include_router(router)
    app.include_router(recommend_router)
    app.include_router(change_router)

    # S4/S4N (Web-UI): static assets + the UI itself, mounted LAST so that
    # /api/v1/* (router) always wins.  Missing dirs degrade gracefully:
    # the mount is skipped (tests / partial checkouts keep working).
    assets_dir = REPO_ROOT / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    ui_dir = REPO_ROOT / "ui"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")

    return app


app = create_app()


def run() -> None:
    """Entry point for the ``cmi-gateway`` console script."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=30001,
        log_level="info",
    )


if __name__ == "__main__":
    run()
