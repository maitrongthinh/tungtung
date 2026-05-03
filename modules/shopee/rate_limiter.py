from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class RateSnapshot:
    rate: float
    capacity: float
    tokens: float


class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._capacity = capacity if capacity is not None else rate
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def snapshot(self) -> RateSnapshot:
        return RateSnapshot(rate=self._rate, capacity=self._capacity, tokens=self._tokens)

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens <= 0:
            return
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_for = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait_for)

    async def throttle_to(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        async with self._lock:
            self._refill()
            self._rate = rate
            self._capacity = max(rate, 1.0)
            self._tokens = min(self._tokens, self._capacity)

    async def reset(self) -> None:
        async with self._lock:
            self._tokens = self._capacity
            self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now
