from __future__ import annotations

import os

import fakeredis
import pytest
import redis

from ratelimiter import ManualClock, MemoryBackend, RateLimiter
from ratelimiter.backends.redis import RedisBackend

# Points at a disposable database: it is flushed around every test that uses it.
REAL_REDIS_URL = os.environ.get("TEST_REDIS_URL")
BACKENDS = ["memory", "redis"] + (["real-redis"] if REAL_REDIS_URL else [])


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=1_700_000_000.0)


@pytest.fixture
def real_redis():
    if not REAL_REDIS_URL:
        pytest.skip("TEST_REDIS_URL is not set")
    client = redis.Redis.from_url(REAL_REDIS_URL)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture(params=BACKENDS)
def backend(request):
    if request.param == "memory":
        return MemoryBackend()
    if request.param == "redis":
        return RedisBackend(fakeredis.FakeRedis())
    return RedisBackend(request.getfixturevalue("real_redis"))


@pytest.fixture
def limiter(backend, clock) -> RateLimiter:
    return RateLimiter(backend, clock=clock)


@pytest.fixture
def memory_limiter(clock) -> RateLimiter:
    return RateLimiter(MemoryBackend(), clock=clock)
