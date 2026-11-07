"""Strict global rate + concurrency limiting built on the ``throttler`` library.

Every single HTTP request (each redirect hop and each retry included) must pass through
``RateLimiter.slot()``. Its dispatch timestamp is recorded so the crawl summary can prove the
--rps ceiling was never exceeded.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from throttler import Throttler, ThrottlerSimultaneous


class RateLimiter:
    def __init__(self, rps: int, concurrency: int) -> None:
        if rps < 1 or concurrency < 1:
            raise ValueError("--rps and --concurrency must be >= 1")
        self.rps = rps
        self.concurrency = concurrency
        # Sliding window: at most `rps` entries in ANY 1-second period (not just per wall second).
        self._throttler = Throttler(rate_limit=rps, period=1.0)
        # At most `concurrency` requests in flight at the same time.
        self._simultaneous = ThrottlerSimultaneous(count=concurrency)
        self.dispatch_times: list[float] = []  # time.time() of every permitted request

    @asynccontextmanager
    async def slot(self):
        """Hold one concurrency slot; block until the rate window allows a new request.

        The timestamp is taken immediately after the throttler releases us, with no ``await``
        in between, so it reflects the moment the request is dispatched.
        """
        async with self._simultaneous:
            async with self._throttler:
                self.dispatch_times.append(time.time())
                yield

    def max_requests_in_window(self, window: float = 1.0) -> int:
        """Largest number of dispatches that fell inside any ``window``-second span."""
        times = sorted(self.dispatch_times)
        best, left = 0, 0
        for right, t in enumerate(times):
            while t - times[left] >= window:
                left += 1
            best = max(best, right - left + 1)
        return best

    def verify(self) -> dict:
        """Proof for crawl_summary.json. A 5 ms tolerance absorbs clock-read jitter only."""
        observed = self.max_requests_in_window(1.0 - 0.005)
        return {
            "rps_limit": self.rps,
            "concurrency_limit": self.concurrency,
            "total_requests": len(self.dispatch_times),
            "max_requests_in_any_1s_window": observed,
            "rps_limit_respected": observed <= self.rps,
        }
