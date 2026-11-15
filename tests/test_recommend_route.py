"""Tests for the L2 recommendation route ``GET /api/v1/recommend``.

The engine is mocked: route-level tests patch ``service.get``; the
cache/TTL/single-flight tests instrument the real service by replacing only
the blocking engine call (``service._run_sync``) with a fake, so the real
caching logic in ``get()`` stays in the loop.  No ``analysis/`` pipeline, no
Open-Meteo network, no CMI.

Coverage:

* fresh: HTTP 200 + the L2 JSON structure + ``generated_at``;
* stale: HTTP 200 with ``stale=true`` inside (design rule R5: stale is data,
  not an error);
* engine failure: HTTP 503 + JSON error body (no bare crash-500);
* caching: a second call inside the TTL does NOT run the engine again;
  after the TTL a fresh run happens; ``force=true`` forces a fresh run;
* token errors: no token = 401, wrong token = 401.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.recommendation import (
    EngineUnavailable,
    RecommendationError,
    RecommendationService,
    berlin_hour_key,
)
from tests.conftest import SCHEMATIC_ASSETS

REPO_ROOT = Path(__file__).resolve().parent.parent

READ = {"Authorization": "Bearer read-tok"}


def _l2_payload(stale: bool = False) -> dict[str, Any]:
    """A minimal L2 v0.1 JSON payload (structure per docs/ai-model-design.md §3.2)."""
    return {
        "ts": "2026-11-02 07:00",
        "model_version": "cmi-brain-v003+l2-v001",
        "horizon_h": 12,
        "stale": stale,
        "stale_reasons": ["metrik-lücke > 1 h"] if stale else [],
        "empfehlungen": [
            {
                "parameter": "RAS-Offset Keller",
                "zone": "kell",
                "kanal": "dev1 a1",
                "aktuell": "+3.0 K",
                "vorgeschlagen": "+1.5 K",
                "wirkung_prognose": "Offset über 12 h gehalten",
                "begruendung_de": "T_out min −2 °C; Keller-Offset senken spart ~0.9 L Öl.",
                "konfidenz": 0.82,
                "executability": "pending-cmi-write-discovery",
                "cmi_hint": "Q-107 (RAS-Offset-Write)",
                "tier": "A",
                "changed": True,
            },
            {
                "parameter": "WW-Ladefenster",
                "zone": None,
                "aktuell": "Puffer-Oben 55.0 °C",
                "vorgeschlagen": "WW im Solar-Fenster erzeugen",
                "wirkung_prognose": "WW im günstigen Fenster",
                "begruendung_de": "Puffer-Oben < 57 °C; Solar-Strahlung 410 Wh/m².",
                "konfidenz": 0.5,
                "executability": "pending-cmi-write-discovery",
                "cmi_hint": "Zeitprogramm-Parameter",
                "tier": "B",
            },
        ],
        "kosten": {"comfort": 0.1, "energy": 0.3},
        "guardrail_hits": [],
        "konfidenz": 0.8,
        "szenarien": [],
        "kontext": {
            "datenstand": "2026-11-02 06:00",
            "state_ts": "2026-11-02 06:00",
            "metric_gap_h": 0.0,
            "data_version": "m1.3",
            "wx_quelle": "Open-Meteo ICON-D2 (live)",
            "wx_run": "2026-11-02T06:00Z",
            "wx_age_h": 0.5,
            "wx_fallback": False,
            "stale": stale,
        },
    }


@pytest.fixture()
def rec_settings(tmp_path: Path) -> Settings:
    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_user="admin",
        cmi_pass="admin",
        cmi_poll_sec=3600,
        cmi_api_interval_sec=0.05,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=SCHEMATIC_ASSETS,
        history_db_path=tmp_path / "data" / "history.db",
        cmi_log_parquet_dir=tmp_path / "data" / "cmi_log_pq",
        gateway_read_token="read-tok",
        gateway_write_token="write-tok",
        audit_jsonl_path=tmp_path / "audit" / "change.jsonl",
        l1_store_dir=tmp_path / "features",
        l1_model_dir=tmp_path / "models" / "v003",
        recommend_cache_min=5,
    )


def _app_client(settings: Settings, service: RecommendationService) -> TestClient:
    """Build the FULL app (so the recommend router is mounted) with the
    given service injected on ``app.state.recommendation``.  The gateway
    pollers are not started (TestClient without context manager would start
    them; we only hit the recommend route, which does no CMI work)."""
    from app.main import create_app

    app = create_app(settings)
    app.state.recommendation = service
    return TestClient(app)


class FakeEngine:
    """Records engine runs; returns a configurable payload or raises."""

    def __init__(self, payload: dict[str, Any] | None = None,
                 error: Exception | None = None, delay: float = 0.0) -> None:
        self.payload = payload or _l2_payload()
        self.error = error
        self.delay = delay
        self.runs = 0
        self.in_flight = 0

    async def get(self, force: bool = False) -> dict[str, Any]:
        import asyncio

        self.runs += 1
        self.in_flight += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            payload = dict(self.payload)
            payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            return payload
        finally:
            self.in_flight -= 1


# ---------------------------------------------------------------------------
# Route level (engine mocked at the service)
# ---------------------------------------------------------------------------
def test_recommend_fresh_200_with_structure(rec_settings: Settings):
    service = RecommendationService(rec_settings)
    fake = FakeEngine()
    service.get = fake.get  # type: ignore[assignment]
    client = _app_client(rec_settings, service)

    r = client.get("/api/v1/recommend", headers=READ)
    assert r.status_code == 200
    body = r.json()
    # exact L2 JSON structure + generated_at
    for key in (
        "ts", "model_version", "horizon_h", "stale", "stale_reasons",
        "empfehlungen", "kosten", "guardrail_hits", "konfidenz",
        "szenarien", "kontext", "generated_at",
    ):
        assert key in body, f"missing key {key!r}"
    assert body["model_version"] == "cmi-brain-v003+l2-v001"
    assert body["horizon_h"] == 12
    assert body["stale"] is False
    assert isinstance(body["empfehlungen"], list) and len(body["empfehlungen"]) == 2
    assert body["generated_at"]
    assert fake.runs == 1


def test_recommend_stale_still_200(rec_settings: Settings):
    """Design rule R5: stale=true is DATA, not an error -> HTTP 200."""
    service = RecommendationService(rec_settings)
    fake = FakeEngine(payload=_l2_payload(stale=True))
    service.get = fake.get  # type: ignore[assignment]
    client = _app_client(rec_settings, service)

    r = client.get("/api/v1/recommend", headers=READ)
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["stale_reasons"]


def test_recommend_engine_error_503_json(rec_settings: Settings):
    """Hard engine failure -> HTTP 503 + JSON error (no bare crash-500)."""
    service = RecommendationService(rec_settings)
    fake = FakeEngine(error=RecommendationError("Store missing"))
    service.get = fake.get  # type: ignore[assignment]
    client = _app_client(rec_settings, service)

    r = client.get("/api/v1/recommend", headers=READ)
    assert r.status_code == 503
    body = r.json()
    assert "error" in body
    assert "Store missing" in body["error"]
    assert body.get("stale") is False
    # no HTML crash page
    assert "text/html" not in r.headers.get("content-type", "")


def test_recommend_requires_read_token(rec_settings: Settings):
    service = RecommendationService(rec_settings)
    fake = FakeEngine()
    service.get = fake.get  # type: ignore[assignment]
    client = _app_client(rec_settings, service)

    assert client.get("/api/v1/recommend").status_code == 401
    assert client.get(
        "/api/v1/recommend", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    # the engine was never run (auth fails first)
    assert fake.runs == 0


# ---------------------------------------------------------------------------
# Caching / single-flight (service level, real cache, fake engine)
# ---------------------------------------------------------------------------
def _instrumented_service(service: RecommendationService, *, stale: bool = False):
    """Instrument a real service: the ENGINE (``_run_sync``) is fake, the
    real cache/TTL/single-flight logic in ``get()`` stays in the loop."""
    fake = FakeEngine(payload=_l2_payload(stale=stale))

    def run_sync(hour: str) -> dict[str, Any]:
        fake.runs += 1
        payload = dict(fake.payload)
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return {"payload": payload, "generated_at": payload["generated_at"]}

    service._run_sync = run_sync  # type: ignore[method-assign]
    return fake


def _asyncio_run(coro_fn):
    import asyncio

    return asyncio.run(coro_fn())


def test_second_call_inside_ttl_no_second_run(rec_settings: Settings):
    service = RecommendationService(rec_settings)
    fake = _instrumented_service(service)

    async def run() -> tuple[dict, dict]:
        p1 = await service.get()
        p2 = await service.get()
        return p1, p2

    p1, p2 = _asyncio_run(run)
    assert fake.runs == 1, "second call inside the TTL must be a cache hit"
    # the cached payload is returned (same generated_at)
    assert p1["generated_at"] == p2["generated_at"]


def test_ttl_expiry_triggers_fresh_run(rec_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    service = RecommendationService(rec_settings)
    fake = _instrumented_service(service)
    monkeypatch.setattr(service, "_ttl_sec", lambda: 0.05)  # 50 ms TTL

    async def run() -> None:
        import asyncio

        await service.get()
        await asyncio.sleep(0.1)  # exceed the 50 ms TTL
        await service.get()

    _asyncio_run(run)
    assert fake.runs == 2, "after the TTL a fresh engine run must happen"


def test_force_true_triggers_fresh_run(rec_settings: Settings):
    service = RecommendationService(rec_settings)
    fake = _instrumented_service(service)

    async def run() -> None:
        await service.get()
        await service.get(force=True)
        await service.get(force=True)

    _asyncio_run(run)
    assert fake.runs == 3, "force=True must always re-run the engine"


def test_concurrent_same_hour_share_one_run(rec_settings: Settings):
    """Concurrent same-hour callers share ONE engine run (single-flight)."""
    service = RecommendationService(rec_settings)
    fake = FakeEngine(payload=_l2_payload())

    def run_sync(hour: str) -> dict[str, Any]:
        fake.runs += 1
        time.sleep(0.05)  # simulate a real engine run
        payload = dict(fake.payload)
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return {"payload": payload, "generated_at": payload["generated_at"]}

    service._run_sync = run_sync  # type: ignore[method-assign]

    async def run():
        import asyncio

        return await asyncio.gather(service.get(), service.get(), service.get())

    results = _asyncio_run(run)
    assert fake.runs == 1, "concurrent same-hour callers must share one run"
    # all callers get the same payload
    assert results[0]["generated_at"] == results[1]["generated_at"] == results[2]["generated_at"]


def test_failed_run_not_cached_retry_succeeds(rec_settings: Settings):
    """A failed run must NOT poison the cache; the next call retries."""
    service = RecommendationService(rec_settings)
    state = {"failed": False}

    def run_sync(hour: str) -> dict[str, Any]:
        if not state["failed"]:
            state["failed"] = True
            raise RecommendationError("transient store error")
        payload = _l2_payload()
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return {"payload": payload, "generated_at": payload["generated_at"]}

    service._run_sync = run_sync  # type: ignore[method-assign]

    async def run():
        with pytest.raises(RecommendationError):
            await service.get()
        return await service.get()  # retry must succeed (no cache poisoning)

    p = _asyncio_run(run)
    assert p["generated_at"]


def test_berlin_hour_key_shape():

    dt = datetime(2026, 11, 2, 7, 45, 12, tzinfo=UTC)
    key = berlin_hour_key(dt)
    # 07:45 UTC = 08:45 Europe/Berlin -> key must be the full BERLIN hour
    assert key == "2026-11-02 08:00"
    # minute/second/microsecond are zeroed
    assert ":00" in key


def test_service_aclose_cancels_inflight(rec_settings: Settings):
    """aclose() cancels a still-running engine task (clean app shutdown)."""
    service = RecommendationService(rec_settings)
    started = {"flag": False}

    def run_sync(hour: str) -> dict[str, Any]:
        started["flag"] = True
        time.sleep(0.3)  # block long enough to be cancelled mid-run
        payload = _l2_payload()
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return {"payload": payload, "generated_at": payload["generated_at"]}

    service._run_sync = run_sync  # type: ignore[method-assign]

    async def run() -> None:
        import asyncio

        task = asyncio.create_task(service.get())
        await asyncio.sleep(0.05)  # let the run start + register in-flight
        assert started["flag"]
        await service.aclose()  # must not hang (cancellation-safe _execute)
        # The get() caller wakes on the (set) event; with no cached result it
        # raises a clean RecommendationError rather than hanging forever.
        with pytest.raises(RecommendationError):
            await task

    _asyncio_run(run)


# ---------------------------------------------------------------------------
# Engine holder / _run_sync (real import + fake predictor)
# ---------------------------------------------------------------------------
def test_engine_ensure_raises_when_analysis_missing(monkeypatch: pytest.MonkeyPatch):
    """A missing analysis/ tree degrades to EngineUnavailable (not a crash)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("analysis"):
            raise ImportError("No module named 'analysis'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    service = RecommendationService(Settings(cmi_base_url="http://x"))
    with pytest.raises(EngineUnavailable):
        service._engine.ensure(service._settings)


def test_engine_predictor_load_failure(rec_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """A broken L1 artifact raises EngineUnavailable (route -> 503)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "analysis.model.m2b.predict":
            raise ImportError("No module named 'analysis.model.m2b'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    service = RecommendationService(rec_settings)
    with pytest.raises(EngineUnavailable):
        service._engine.predictor(rec_settings)


def test_engine_predictor_cached(rec_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """The predictor is loaded once and cached on the holder."""
    import builtins

    real_import = builtins.__import__

    class FakePredictor:
        pass

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "analysis.model.m2b.predict":
            module = type(
                "mod",
                (),
                {"L1Predictor": type("P", (), {"load": staticmethod(lambda d: FakePredictor())})},
            )
            return module
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    service = RecommendationService(rec_settings)
    p1 = service._engine.predictor(rec_settings)
    p2 = service._engine.predictor(rec_settings)
    assert p1 is p2
    assert isinstance(p1, FakePredictor)


def test_run_sync_wraps_engine_failure(rec_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """_run_sync wraps any engine failure in RecommendationError (-> 503)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "analysis.model.l2.recommend":
            return type("mod", (), {"recommend": staticmethod(_boom)})
        if name == "analysis.model.l2":
            return type("mod", (), {})
        return real_import(name, *args, **kwargs)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("engine crash")

    monkeypatch.setattr(builtins, "__import__", fake_import)
    service = RecommendationService(rec_settings)
    service._engine.predictor = lambda settings: object()  # type: ignore[method-assign]
    with pytest.raises(RecommendationError, match="engine crash"):
        service._run_sync("2026-11-02 07:00")


def test_run_sync_success_adds_generated_at(rec_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """_run_sync returns payload + generated_at on a successful engine run."""
    import builtins

    real_import = builtins.__import__

    class OkRecommendation:
        def to_json(self) -> dict[str, Any]:
            return {"ts": "2026-11-02 07:00", "stale": False}

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "analysis.model.l2.recommend":
            return type("mod", (), {"recommend": staticmethod(lambda *a, **k: OkRecommendation())})
        if name == "analysis.model.l2":
            return type("mod", (), {})
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    service = RecommendationService(rec_settings)
    service._engine.predictor = lambda settings: object()  # type: ignore[method-assign]
    entry = service._run_sync("2026-11-02 07:00")
    assert "generated_at" in entry["payload"]
    assert entry["payload"]["ts"] == "2026-11-02 07:00"
    assert entry["generated_at"] == entry["payload"]["generated_at"]


# ---------------------------------------------------------------------------
# Cache property / invalidate
# ---------------------------------------------------------------------------
def test_cache_property_and_invalidate(rec_settings: Settings):
    service = RecommendationService(rec_settings)
    assert service.cache == {}
    service.cache["h1"] = {"payload": {"ts": "x"}, "_at": 0.0}
    service.cache["h2"] = {"payload": {"ts": "y"}, "_at": 0.0}
    service.invalidate("h1")
    assert "h1" not in service.cache
    assert "h2" in service.cache
    service.invalidate()  # drop all
    assert service.cache == {}
