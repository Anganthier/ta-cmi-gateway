"""Poller tests: success + failure paths of the background tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import httpx
import pytest

from app.cmi.changer_resolver import ChangerResolver
from app.cmi.client import CMIClient
from app.cmi.queue import CMIGuard
from app.db import now_iso
from app.main import Gateway
from app.state import StateStore
from tests.conftest import make_fake_transport
from tests.test_cmi_log import LogServer, _log_gateway, make_log_file


def _gateway(settings, transport: httpx.MockTransport, *, store: StateStore | None = None,
             api_start_delay: float = 0.0) -> Gateway:
    client = CMIClient(
        base_url=settings.base_url,
        user=settings.cmi_user,
        password=settings.cmi_pass,
        transport=transport,
    )
    guard = CMIGuard(api_min_interval_sec=settings.cmi_api_interval_sec)
    return Gateway(
        settings,
        client=client,
        guard=guard,
        store=store or StateStore(cmi_url=settings.base_url),
        changer=ChangerResolver(client, guard, ttl_sec=60, cache_dir=settings.cmi_data_dir),
        api_start_delay=api_start_delay,
    )


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("poller did not satisfy condition in time")


@pytest.mark.asyncio
async def test_schematic_poller_populates_store(settings):
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport())
    task = asyncio.create_task(gateway._schematic_poller())
    try:
        await _wait_for(lambda: len(gateway.store.snapshot().schematics) == 5, timeout=3.0)
        doc = gateway.store.snapshot()
        assert doc.health.schematic_ok is True
        assert doc.health.schematic_error is None
        assert doc.fields, "schematic fields expected"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_api_poller_populates_store(settings):
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport(), api_start_delay=0.0)
    task = asyncio.create_task(gateway._api_poller())
    try:
        await _wait_for(lambda: len(gateway.store.api_results) == 2, timeout=3.0)
        doc = gateway.store.snapshot()
        assert doc.health.api_ok is True
        assert set(gateway.store.api_results) == {1, 32}
        api_fields = [f for f in doc.fields if f.source == "json_api"]
        assert len(api_fields) >= 20
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_schematic_poller_survives_errors(settings):
    """CMI 500 responses: poller logs, marks health, keeps running."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    gateway = _gateway(settings, httpx.MockTransport(handler))
    task = asyncio.create_task(gateway._schematic_poller())
    try:
        await _wait_for(
            lambda: bool(gateway.store.health.schematic_error), timeout=3.0
        )
        assert "500" in (gateway.store.health.schematic_error or "")
        # last values stay empty but health time is set
        assert gateway.store.health.schematic_time is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_api_poller_rate_limited_backoff(settings):
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {"Header": {}, "Data": {}, "Status": "TOO MANY REQUESTS", "Status code": 4}
            ),
        )

    gateway = _gateway(settings, httpx.MockTransport(handler), api_start_delay=0.0)
    task = asyncio.create_task(gateway._api_poller())
    try:
        await _wait_for(
            lambda: bool(gateway.store.health.api_error), timeout=3.0
        )
        assert "4" in (gateway.store.health.api_error or "")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_schematic_poller_survives_unexpected_parser_error(settings, monkeypatch):
    """Defect 5d: a non-CMI exception (deliberately broken parser call) must not
    kill the poller; it must keep running and surface the failure on the health
    route via the ``unexpected error`` marker."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport())

    def broken_parser(*_a, **_k):
        raise RuntimeError("broken parser (non-CMI)")

    monkeypatch.setattr("app.main.load_schematic", broken_parser)
    task = asyncio.create_task(gateway._schematic_poller())
    try:
        await _wait_for(
            lambda: "unexpected error" in (gateway.store.health.schematic_error or ""),
            timeout=3.0,
        )
        assert gateway.store.health.schematic_ok is False
        # The poller survived the error and is still alive (looping).
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_api_poller_survives_unexpected_error(settings, monkeypatch):
    """Defect 5d: a non-CMI exception from the JSON-API layer must not kill the
    API poller; the health route must flag the unexpected error."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport(), api_start_delay=0.0)

    async def broken_fetch(*_a, **_k):
        raise RuntimeError("non-CMI failure")

    monkeypatch.setattr(gateway.json_api, "fetch", broken_fetch)
    task = asyncio.create_task(gateway._api_poller())
    try:
        await _wait_for(
            lambda: "unexpected error" in (gateway.store.health.api_error or ""),
            timeout=3.0,
        )
        assert gateway.store.health.api_ok is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_start_stop_pollers_idempotent(settings):
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport())
    gateway.start_pollers()
    n_tasks = len(gateway._tasks)
    assert n_tasks == 4  # schematic + JSON-API + CMI /LOG + Minderer-Loop Poller
    gateway.start_pollers()  # idempotent
    assert len(gateway._tasks) == n_tasks
    assert all(not t.done() for t in gateway._tasks)
    await gateway.stop_pollers()
    assert gateway._tasks == []
    await gateway.aclose()


# ---------------------------------------------------------------------------
# S2-1: poller -> history persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_schematic_poller_persists_to_history(settings):
    """Each successful schematic poll must write rows into data/history.db."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport())
    task = asyncio.create_task(gateway._schematic_poller())
    try:
        # wait until at least one page was persisted (pages 2..5 have numeric blocks)
        await _wait_for(
            lambda: gateway.history.stats()["rows"] > 0, timeout=5.0
        )
        stat = gateway.history.stats()
        assert stat["rows"] > 0
        # rows are tagged with source 'schematic'
        con = gateway.history._ensure_conn()
        n_schematic = con.execute(
            "SELECT COUNT(*) FROM measurements WHERE source='schematic'"
        ).fetchone()[0]
        assert n_schematic > 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_api_poller_persists_to_history(settings):
    """Each successful JSON-API poll must write rows tagged 'json:N'."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport(), api_start_delay=0.0)
    task = asyncio.create_task(gateway._api_poller())
    try:
        await _wait_for(
            lambda: gateway.history.stats()["rows"] > 0, timeout=5.0
        )
        con = gateway.history._ensure_conn()
        json_rows = con.execute(
            "SELECT COUNT(*) FROM measurements WHERE source='json'"
        ).fetchone()[0]
        assert json_rows > 0
        # device names are 'json:<node>'
        devices = {r[0] for r in con.execute("SELECT DISTINCT device FROM measurements")}
        assert any(d.startswith("json:") for d in devices)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


@pytest.mark.asyncio
async def test_history_db_error_does_not_crash_poller(settings, monkeypatch):
    """A DB write error must not crash the schematic poller; it latches a
    history_error flag instead (best-effort persistence)."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = _gateway(settings, make_fake_transport())

    def boom_upsert(self, rows, *, source_name="schematic"):
        raise sqlite3.DatabaseError("disk full")

    monkeypatch.setattr("app.db.HistoryDB.upsert", boom_upsert)
    task = asyncio.create_task(gateway._schematic_poller())
    try:
        # The poller should still populate the state store (CMI route works)
        # even though history persistence fails.
        await _wait_for(
            lambda: len(gateway.store.snapshot().schematics) > 0, timeout=5.0
        )
        assert not task.done(), "poller must survive a DB error"
        # the error is latched on the gateway, not raised
        assert gateway._history_error and "disk full" in gateway._history_error
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await gateway.aclose()


# ---------------------------------------------------------------------------
# S4N-LogParquet A: CMI /LOG poller is read-only by default (CMI_LOG_CLEAR)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cmi_log_default_read_only_ingests_without_clear(settings):
    """Default (CMI_LOG_CLEAR=False): a NEW index file is ingested (Parquet
    write + side-table mark), but the mock transport must receive NO
    /LOG/clear.log at all -- the poller is strictly read-only."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2])
    assert settings.cmi_log_clear is False  # the config default under test
    gw = _log_gateway(settings, srv)
    try:
        # f1 was ingested by an earlier cycle (side-table mark) -> only f2 is
        # new this cycle.
        gw.history.cmi_log_mark(f1[0].rsplit("/", 1)[-1], f1[1], "old-sha", now_iso())
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [f2[0]]  # only the new file was fetched
        assert srv.calls["clear"] == 0  # read-only: clear.log NEVER sent
        # Parquet write happened (new file's 39 records in the month file)...
        import pyarrow.parquet as pq

        table = pq.read_table(
            settings.cmi_log_parquet_dir / "2026" / "10" / "2026-10.parquet"
        )
        assert len(table) == 39
        # ...and the file is marked in the side table (dedup for next cycle).
        assert set(gw.history.cmi_log_ingested_names()) == {
            f1[0].rsplit("/", 1)[-1], f2[0].rsplit("/", 1)[-1]
        }
        size, sha, _ts = gw.history.cmi_log_get(f2[0].rsplit("/", 1)[-1])
        assert size == f2[1]
        assert sha == hashlib.sha256(f2[2]).hexdigest()
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_clear_flag_on_issues_clear_when_all_ingested(settings):
    """CMI_LOG_CLEAR=True: the legacy last-data-first gate is kept -- clear
    only when EVERY index file is ingested."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2])
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        gw.history.cmi_log_mark(f1[0].rsplit("/", 1)[-1], f1[1], "old-sha", now_iso())
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [f2[0]]  # only the new file was fetched
        assert srv.calls["clear"] == 1  # all files ingested -> legacy gate clears
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_clear_flag_on_no_clear_when_not_all_ingested(settings):
    """CMI_LOG_CLEAR=True, one download fails: NOT all index files are
    ingested -> no clear (last-data-first)."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2], data_fail={f2[0]: 500})
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert srv.calls["clear"] == 0
        assert gw.history.cmi_log_ingested_names() == {f1[0].rsplit("/", 1)[-1]}
        assert gw._cmi_log_error is None  # per-file failure is swallowed
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_empty_index_updates_last_poll_ts(settings):
    """Health-fix: a successful cycle with an EMPTY index still refreshes
    last_poll_ts on /health (previously the early-return left a stale ts)."""
    srv = LogServer([])  # index 200, zero data files
    gw = _log_gateway(settings, srv)
    try:
        assert gw.cmi_log_status()["last_poll_ts"] is None
        await gw._cmi_log_cycle()
        st = gw.cmi_log_status()
        assert st["error"] is None
        assert st["last_poll_ts"] is not None  # ts refresh even when nothing new
        assert st["last_ingested_files"] == 0  # semantics unchanged
        assert srv.calls["clear"] == 0
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_persist_failure_latches_without_clear(settings, monkeypatch):
    """Persist error path (CMI_LOG_CLEAR=True): a Parquet write failure must
    latch the error, NOT mark the file ingested, and keep the clear gate
    closed for that cycle."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1])
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        def boom_write_month(month: str, records):
            raise OSError("simulated: cmi_log_pq dir read-only (disk full)")

        monkeypatch.setattr(gw.cmi_log_pq, "write_month", boom_write_month)
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is not None and "cmi-log" in gw._cmi_log_error
        assert gw._cmi_log_persist_failed is True
        # file is NOT ingested -> next cycle retries it; clear stayed closed.
        assert gw.history.cmi_log_ingested_names() == set()
        assert srv.calls["clear"] == 0
        # Health-fix semantics: a failed cycle does NOT refresh last_poll_ts.
        assert gw.cmi_log_status()["last_poll_ts"] is None
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# S4N-LogPoll-DedupFix: name+SIZE dedup (growing active block)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cmi_log_grown_active_block_refetched_size_upgraded(settings):
    """Name+SIZE dedup (S4N-LogPoll-DedupFix): an index entry whose size grew
    since the last ingestion (the CMI's active block keeps being appended
    while it is listed in the index) is re-fetched; the Parquet month write
    receives the NEW records (ts-union) and the side-table size is upgraded
    to the fresh index size via cmi_log_mark's INSERT OR REPLACE."""
    name = "data_2026_10_27_00_00_04.log"
    p, s_a, b_a = make_log_file(name, (0, 0, 4), n_records=40)
    # The active block "grew": same name, more records, bigger body/size.
    p2, s_b, b_b = make_log_file(name, (0, 0, 4), n_records=80)
    assert s_b > s_a
    srv = LogServer([(p, s_a, b_a)])
    gw = _log_gateway(settings, srv)
    try:
        # Cycle 1: ingest at size A (39 decoded records, record 0 skipped).
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [p]
        assert gw.history.cmi_log_ingested_meta() == {name: s_a}
        import pyarrow.parquet as pq

        table1 = pq.read_table(
            settings.cmi_log_parquet_dir / "2026" / "10" / "2026-10.parquet"
        )
        assert len(table1) == 39

        # The CMI appended more records to the active block: the index now
        # reports the bigger size for the SAME file.
        srv.files[p] = (s_b, b_b)

        # Cycle 2: stored size A != index size B -> re-fetch, ts-dedup merge,
        # side-table size upgraded to B.
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [p, p]  # re-fetched, not skipped
        assert srv.calls["clear"] == 0
        table2 = pq.read_table(
            settings.cmi_log_parquet_dir / "2026" / "10" / "2026-10.parquet"
        )
        # ts-union: the 39 old ts stay + the 40 new records are appended.
        assert len(table2) == 79
        # cmi_log_mark overwrote the side-table row (INSERT OR REPLACE):
        # size == new index size, sha == new body fingerprint.
        assert gw.history.cmi_log_ingested_meta() == {name: s_b}
        size, sha, _ts = gw.history.cmi_log_get(name)
        assert size == s_b
        assert sha == hashlib.sha256(b_b).hexdigest()
        assert gw._cmi_log_error is None

        # Cycle 3: size matches again -> nothing is re-fetched (no busy loop).
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [p, p]  # no third download
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_clear_gate_blocks_on_grown_file(settings, monkeypatch):
    """Clear gate (CMI_LOG_CLEAR=True) + grown active block: the side table
    holds the file at the OLD index size A, the index now reports B > A, and
    this cycle's re-fetch cannot persist (Parquet write fails) -> the gate
    must NOT clear, because no file is persisted at the current index size
    (exact condition: meta[name] == f.size for every index file)."""
    name = "data_2026_10_27_00_00_04.log"
    p, s_a, b_a = make_log_file(name, (0, 0, 4), n_records=40)
    p2, s_b, b_b = make_log_file(name, (0, 0, 4), n_records=80)
    assert s_b > s_a
    srv = LogServer([(p, s_b, b_b)])  # index already reports the grown size
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        # Previous cycle persisted the file at the smaller index size A.
        gw.history.cmi_log_mark(name, s_a, hashlib.sha256(b_a).hexdigest(), now_iso())
        assert gw.history.cmi_log_ingested_meta() == {name: s_a}

        def boom_write_month(month: str, records):
            raise OSError("simulated: cmi_log_pq dir read-only (disk full)")

        monkeypatch.setattr(gw.cmi_log_pq, "write_month", boom_write_month)
        await gw._cmi_log_cycle()
        # The grown file WAS re-fetched (name+size dedup detected B != A)...
        assert srv.calls["data"] == [p]
        # ...but its persist failed, so it is NOT marked at size B -> the
        # exact gate condition (meta[name] == f.size) fails -> NO clear.
        assert srv.calls["clear"] == 0
        assert gw.history.cmi_log_ingested_meta() == {name: s_a}  # still A
        assert gw._cmi_log_error is not None  # latch stays visible
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# S4N-LogFlush: RAM-buffer flush-read (GET /LOG/infoh.log) before the index
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cmi_log_flush_read_before_index_read(settings):
    """S4N-LogFlush: the cycle starts with exactly ONE GET /LOG/infoh.log
    (the CMI's RAM-buffer flush trigger, WinSol "Logger auslesen"
    semantics) BEFORE the year-index read; ingestion then proceeds as
    before."""
    import datetime as _dt

    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1])
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        # The flush-read happened exactly once, on the flush-read route
        # (counted in calls["infoh"], NOT in the index counter)...
        assert srv.calls["infoh"] == 1
        # ...it strictly precedes the year-index read in the observed
        # request order (gateway is sequential; both counters grow 1:1)...
        assert srv.calls["index"] == 1
        assert srv.request_paths.index("/LOG/infoh.log") < srv.request_paths.index(
            f"/LOG/info{_dt.datetime.now().year}.log"
        )
        # ...and the cycle ingested the index file as before.
        assert srv.calls["data"] == [f1[0]]
        assert gw.history.cmi_log_ingested_names() == {f1[0].rsplit("/", 1)[-1]}
        assert gw._cmi_log_error is None
        # A second cycle repeats the pattern 1:1 -- one flush-read per
        # cycle, strictly before its index read (gateway is sequential, so
        # both counters grow together; no double-flush).
        await gw._cmi_log_cycle()
        assert srv.calls["infoh"] == 2
        assert srv.calls["index"] == 2
        assert srv.calls["data"] == [f1[0]]  # dedup: nothing new
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_flush_read_error_does_not_block_cycle(settings, caplog):
    """S4N-LogFlush (best-effort): a failing infoh.log read (here HTTP 500)
    must NOT block the cycle -- the index read and the ingestion still
    happen, nothing is latched (_cmi_log_error stays None), and the failure
    is only a warning log line."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1], infoh_status=500)
    gw = _log_gateway(settings, srv)
    try:
        with caplog.at_level("WARNING", logger="cmi.gateway"):
            await gw._cmi_log_cycle()
        # The flush-read was attempted (counted) and failed...
        assert srv.calls["infoh"] == 1
        # ...but the cycle ran on: index read + ingestion, no latch.
        assert srv.calls["index"] == 1
        assert srv.calls["data"] == [f1[0]]
        assert gw.history.cmi_log_ingested_names() == {f1[0].rsplit("/", 1)[-1]}
        assert gw._cmi_log_error is None
        assert gw.cmi_log_status()["error"] is None
        # The failure is visible as a warning, not as a health error.
        assert any("flush-read infoh.log failed" in r.message for r in caplog.records)
        # A subsequent healthy cycle (flush-read now 200) stays clean.
        srv.infoh_status = 0
        await gw._cmi_log_cycle()
        assert srv.calls["infoh"] == 2
        assert srv.calls["index"] == 2
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_cmi_log_flush_read_once_on_year_fallback(settings):
    """S4N-LogFlush + year-boundary fallback: infoh.log is year-INDEPENDENT,
    so the fallback path (current-year index 404 -> previous-year index)
    must issue the flush-read exactly ONCE per cycle -- not again in the
    fallback branch; the fallback itself is unchanged (previous-year file
    ingested, no clear)."""
    import datetime as _dt

    now_year = _dt.datetime.now().year
    prev_year = now_year - 1
    f_old = make_log_file(
        f"data_{prev_year}_12_31_23_50_00.log", (23, 50, 0), year=prev_year
    )
    srv = LogServer(
        [f_old],
        index_404_years=frozenset({now_year}),
    )
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        # One flush-read for the whole cycle, even though the index read
        # itself happened twice (current year 404 + fallback year).
        assert srv.calls["infoh"] == 1
        assert srv.calls["index"] == 2  # year + fallback-year index
        assert srv.calls["data"] == [f_old[0]]
        assert srv.calls["clear"] == 0  # no clear across the year boundary
        assert gw.history.cmi_log_ingested_names() == {f_old[0].rsplit("/", 1)[-1]}
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()
