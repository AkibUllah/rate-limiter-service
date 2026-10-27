"""`RateLimiter.peek`: read the state of a key without consuming, on both backends."""

from __future__ import annotations

import fakeredis
import pytest

from ratelimiter import Algorithm, ManualClock, MemoryBackend, Policy, RateLimiter
from ratelimiter.backends.redis import RedisBackend

ALL = list(Algorithm)


def _new_backend(kind):
    return MemoryBackend() if kind == "memory" else RedisBackend(fakeredis.FakeRedis())


@pytest.fixture(params=["memory", "redis"])
def backend_kind(request):
    return request.param


@pytest.mark.parametrize("algorithm", ALL)
def test_peek_on_a_fresh_key_reports_full_budget_and_stores_nothing(backend_kind, algorithm):
    backend = _new_backend(backend_kind)
    limiter = RateLimiter(backend, clock=ManualClock(start=1_700_000_000.0))
    policy = Policy(limit=5, window=10, algorithm=algorithm)

    for _ in range(3):
        d = limiter.peek("fresh", policy)
        assert d.allowed and d.remaining == 5 and d.retry_after == 0

    if backend_kind == "memory":
        assert len(backend) == 0
    else:
        assert backend._redis.keys() == []


@pytest.mark.parametrize("algorithm", ALL)
def test_peek_shows_remaining_without_consuming(limiter, algorithm):
    policy = Policy(limit=5, window=10, algorithm=algorithm)
    for _ in range(3):
        limiter.check("k", policy)
    for _ in range(10):
        d = limiter.peek("k", policy)
        assert d.allowed and d.remaining == 2
    assert [limiter.check("k", policy).allowed for _ in range(3)] == [True, True, False]


@pytest.mark.parametrize("algorithm", ALL)
def test_peek_on_exhausted_key_matches_the_next_check(limiter, algorithm):
    policy = Policy(limit=3, window=30, algorithm=algorithm)
    for _ in range(3):
        limiter.check("k", policy)
    peeked = limiter.peek("k", policy)
    checked = limiter.check("k", policy)
    assert not peeked.allowed and not checked.allowed
    assert peeked.remaining == 0
    assert peeked.retry_after == pytest.approx(checked.retry_after)
    assert peeked.reset_after == pytest.approx(checked.reset_after)


@pytest.mark.parametrize("algorithm", ALL)
def test_peek_respects_cost(limiter, algorithm):
    policy = Policy(limit=10, window=60, algorithm=algorithm)
    limiter.check("k", policy, cost=7)
    assert limiter.peek("k", policy, cost=3).allowed
    assert not limiter.peek("k", policy, cost=4).allowed
    assert limiter.peek("k", policy, cost=4).retry_after > 0
    with pytest.raises(ValueError):
        limiter.peek("k", policy, cost=0)


@pytest.mark.parametrize("algorithm", ALL)
def test_interleaved_peeks_never_change_later_decisions(backend_kind, algorithm):
    """Two identical limiters, one of them peeked at constantly, must decide identically."""
    policy = Policy(limit=4, window=10, algorithm=algorithm, burst=6)
    clocks = [ManualClock(start=1_700_000_000.0), ManualClock(start=1_700_000_000.0)]
    plain, peeked = (RateLimiter(_new_backend(backend_kind), clock=c) for c in clocks)

    for step in range(60):
        for _ in range(3):
            peeked.peek("k", policy)
        a, b = plain.check("k", policy), peeked.check("k", policy)
        assert (a.allowed, a.remaining) == (b.allowed, b.remaining), step
        assert a.retry_after == pytest.approx(b.retry_after)
        for clock in clocks:
            clock.advance(0.7)


def test_redis_peek_leaves_expired_log_entries_in_place(clock):
    client = fakeredis.FakeRedis()
    limiter = RateLimiter(RedisBackend(client), clock=clock)
    policy = Policy(limit=3, window=10, algorithm=Algorithm.SLIDING_LOG, name="otp")
    limiter.check("k", policy)
    clock.advance(6)
    limiter.check("k", policy)
    clock.advance(5)  # the first entry is now expired but still stored

    skey = limiter.storage_key("k", policy)
    assert client.zcard(skey) == 2
    d = limiter.peek("k", policy)
    assert d.allowed and d.remaining == 2
    assert d.reset_after == pytest.approx(5.0)  # the surviving entry expires in 5s
    assert client.zcard(skey) == 2
