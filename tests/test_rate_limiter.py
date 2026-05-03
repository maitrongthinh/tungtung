import asyncio
import time

import pytest

from modules.shopee.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_token_bucket_throttles_concurrent_acquires() -> None:
    limiter = TokenBucketRateLimiter(rate=5, capacity=1)
    start = time.perf_counter()
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    elapsed = time.perf_counter() - start
    assert elapsed >= 0.35


@pytest.mark.asyncio
async def test_token_bucket_can_degrade_rate() -> None:
    limiter = TokenBucketRateLimiter(rate=10, capacity=1)
    await limiter.acquire()
    await limiter.throttle_to(2)
    start = time.perf_counter()
    await limiter.acquire()
    elapsed = time.perf_counter() - start
    assert elapsed >= 0.45
