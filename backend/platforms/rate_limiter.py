"""Token-bucket rate limiter for API request throttling."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Async rate limiter using token-bucket algorithm.

    Usage:
        limiter = RateLimiter(rps=5, burst=10)
        async with limiter:
            await make_request()
    """

    def __init__(self, rps: int = 5, burst: int | None = None) -> None:
        self.rps = rps
        self.burst = burst or rps
        self._tokens: float = float(self.burst)
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._waiters: asyncio.Queue[asyncio.Event] = asyncio.Queue()

    async def acquire(self) -> None:
        """Acquire a token, blocking if none available."""
        if self.rps <= 0:
            return  # disabled

        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

        # No tokens available — wait for refill
        event = asyncio.Event()
        await self._waiters.put(event)
        await event.wait()

        async with self._lock:
            self._refill()
            self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rps)
        self._last_refill = now

        # Wake up any waiters that can now acquire
        while not self._waiters.empty() and self._tokens >= 1.0:
            try:
                waiter = self._waiters.get_nowait()
                waiter.set()
            except asyncio.QueueEmpty:
                break

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *args: object) -> None:
        pass
