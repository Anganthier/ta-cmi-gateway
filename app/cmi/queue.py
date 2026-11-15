"""Serial CMI queue and JSON-API rate limiter.

The CMI tolerates exactly **one outstanding request at a time** in practice,
and its JSON-API (``/INCLUDE/api.cgi``) is hard-limited to one request per
minute.  This module provides:

* :class:`CMISerialQueue` -- a single-flight guarantee: only one request
  runs at a time, all others queue behind it, each with its own timeout.
* :class:`ApiRateLimiter` -- enforces a minimum interval (default 65 s)
  between two ``api.cgi`` calls.
* :class:`CMIGuard` -- composition of both, the object the gateway uses for
  every CMI interaction.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class CMISerialQueue:
    """Run at most one coroutine at a time; others wait in FIFO order.

    Each queued call is protected by a per-request timeout so that a wedged
    request cannot stall the queue indefinitely.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self._completed = 0

    @property
    def in_flight(self) -> int:
        """Number of calls currently executing (0 or 1)."""
        return self._in_flight

    @property
    def completed(self) -> int:
        """Total number of calls that finished (successfully or not)."""
        return self._completed

    async def run(self, func: Callable[[], Awaitable[T]], *, timeout: float = 30.0) -> T:
        """Execute ``func`` under the single-flight guarantee.

        Args:
            func: Zero-argument async callable.
            timeout: Per-request timeout in seconds; raises
                :class:`asyncio.TimeoutError` when exceeded.
        """
        async with self._lock:
            self._in_flight = 1
            try:
                return await asyncio.wait_for(func(), timeout=timeout)
            finally:
                self._in_flight = 0
                self._completed += 1


class ApiRateLimiter:
    """Guarantee a minimum interval between two JSON-API calls.

    The limiter is process-wide (single event loop).  ``acquire()`` returns
    immediately when enough time has passed since the last call, otherwise
    it sleeps until the interval is satisfied.
    """

    def __init__(self, min_interval_sec: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if min_interval_sec < 0:
            raise ValueError("min_interval_sec must be >= 0")
        if min_interval_sec < 0:
            raise ValueError("min_interval_sec must be >= 0")
        self._min_interval = float(min_interval_sec)
        self._clock = clock
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()
        self._acquired = 0

    @property
    def min_interval_sec(self) -> float:
        return self._min_interval

    @property
    def calls(self) -> int:
        """Number of acquired slots so far (for tests/observability)."""
        return self._acquired

    async def acquire(self) -> None:
        """Wait until the next api.cgi call may start."""
        async with self._lock:
            now = self._clock()
            if now < self._next_allowed_at:
                await asyncio.sleep(self._next_allowed_at - now)
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval
            self._acquired += 1


class CMIGuard:
    """Serial queue + api.cgi rate limiter combined for gateway use."""

    def __init__(self, api_min_interval_sec: float = 65.0, *, default_timeout: float = 30.0) -> None:
        self.queue = CMISerialQueue()
        self.api_limiter = ApiRateLimiter(api_min_interval_sec)
        self._default_timeout = default_timeout
        self.last_api_error: str | None = None

    async def run_schematic(self, func: Callable[[], Awaitable[T]], *, timeout: float | None = None) -> T:
        """Run a schematic (or other unlimited) request through the queue."""
        return await self.queue.run(func, timeout=self._default_timeout if timeout is None else timeout)

    async def run_api(self, func: Callable[[], Awaitable[T]]) -> T:
        """Run a JSON-API request: rate-limit first, then serial execution."""
        await self.api_limiter.acquire()
        try:
            return await self.queue.run(func, timeout=self._default_timeout)
        except BaseException:
            # After a failure the slot is already consumed; the limiter keeps
            # its interval based on the recorded call.
            raise
