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

    async def acquire(self) -> None:
        """Acquire a token, blocking if none available."""
        if self.rps <= 0:
            return  # disabled

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            # No token available — wait one refill interval and retry.
            # (Do NOT wait on an event that only fires on the next acquire();
            #  that deadlocks once the burst is exhausted and all callers are blocked.)
            await asyncio.sleep(1.0 / self.rps)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rps)
        self._last_refill = now

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *args: object) -> None:
        pass
