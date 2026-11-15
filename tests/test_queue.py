"""Unit tests for the serial CMI queue and the JSON-API rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.cmi.queue import ApiRateLimiter, CMIGuard, CMISerialQueue


@pytest.mark.asyncio
async def test_serial_queue_single_flight():
    """Two concurrent callers must execute strictly one after the other."""
    queue = CMISerialQueue()
    running: list[str] = []
    order: list[str] = []

    async def job(name: str, duration: float) -> None:
        running.append(name)
        order.append(f"start-{name}")
        await asyncio.sleep(duration)
        assert list(running) == [name], f"concurrent execution: {running}"
        order.append(f"end-{name}")
        running.remove(name)

    await asyncio.gather(
        queue.run(lambda: job("a", 0.2)),
        queue.run(lambda: job("b", 0.2)),
    )
    assert order in (
        ["start-a", "end-a", "start-b", "end-b"],
        ["start-b", "end-b", "start-a", "end-a"],
    )
    assert queue.completed == 2
    assert queue.in_flight == 0


@pytest.mark.asyncio
async def test_serial_queue_serial_with_three_callers():
    queue = CMISerialQueue()
    active = 0
    max_active = 0

    async def bump() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    await asyncio.gather(*[queue.run(bump) for _ in range(3)])
    assert max_active == 1
    assert queue.completed == 3


@pytest.mark.asyncio
async def test_serial_queue_timeout():
    queue = CMISerialQueue()

    async def slow() -> None:
        await asyncio.sleep(1.0)

    with pytest.raises(asyncio.TimeoutError):
        await queue.run(slow, timeout=0.05)
    assert queue.completed == 1
    assert queue.in_flight == 0


@pytest.mark.asyncio
async def test_serial_queue_releases_after_failure():
    queue = CMISerialQueue()

    async def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await queue.run(fail, timeout=5)

    async def ok() -> str:
        return "fine"

    assert await queue.run(ok, timeout=5) == "fine"
    assert queue.completed == 2


def test_rate_limiter_invalid_interval():
    with pytest.raises(ValueError):
        ApiRateLimiter(min_interval_sec=-1)


@pytest.mark.asyncio
async def test_rate_limiter_enforces_interval():
    limiter = ApiRateLimiter(0.3)
    t0 = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    elapsed = time.monotonic() - t0
    # second acquire must wait ~0.3 s
    assert elapsed >= 0.28, f"rate limit not enforced, elapsed={elapsed:.3f}"
    assert limiter.calls == 2


@pytest.mark.asyncio
async def test_rate_limiter_first_call_immediate():
    limiter = ApiRateLimiter(60.0)
    t0 = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - t0 < 0.1


@pytest.mark.asyncio
async def test_guard_runs_api_through_queue_and_limiter():
    guard = CMIGuard(api_min_interval_sec=0.05)
    active = 0
    max_active = 0
    calls: list[float] = []
    clock = time.monotonic

    async def api_call() -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(clock())
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    await guard.run_api(api_call)
    await guard.run_api(api_call)
    assert max_active == 1
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.04  # interval respected


@pytest.mark.asyncio
async def test_guard_schematic_and_api_never_overlap():
    guard = CMIGuard(api_min_interval_sec=0.0)
    active = 0
    max_active = 0

    async def work() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1

    await asyncio.gather(
        guard.run_schematic(work),
        guard.run_api(work),
    )
    assert max_active == 1
