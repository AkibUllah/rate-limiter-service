from __future__ import annotations

import fakeredis
import pytest

from ratelimiter import ManualClock, MemoryBackend, RateLimiter
from ratelimiter.backends.redis import RedisBackend


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=1_700_000_000.0)


@pytest.fixture(params=["memory", "redis"])
def backend(request):
    if request.param == "memory":
        return MemoryBackend()
    return RedisBackend(fakeredis.FakeRedis())


@pytest.fixture
def limiter(backend, clock) -> RateLimiter:
    return RateLimiter(backend, clock=clock)


@pytest.fixture
def memory_limiter(clock) -> RateLimiter:
    return RateLimiter(MemoryBackend(), clock=clock)
