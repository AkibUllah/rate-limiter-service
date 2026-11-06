"""`RateLimiter.check_all`: several policies, one atomic decision, on every backend."""

from __future__ import annotations

import itertools
import random
import threading

import fakeredis
import pytest

from ratelimiter import Algorithm, ManualClock, MemoryBackend, Policy, RateLimiter
from ratelimiter.backends.redis import RedisBackend

ALL = list(Algorithm)
PAIRS = list(itertools.product(ALL, ALL))


def pair(first, second, tight=2, wide=100):
    """A tight policy and a wide one, both over 60 seconds, that never share state."""
    return [
        Policy(limit=tight, window=60, algorithm=first, name="tight"),
        Policy(limit=wide, window=60, algorithm=second, name="wide"),
    ]


def stored_state(backend) -> int:
    if isinstance(backend, MemoryBackend):
        return len(backend)
    return len(backend._redis.keys())


@pytest.mark.parametrize(("first", "second"), PAIRS)
def test_denied_check_consumes_nothing_from_any_policy(limiter, first, second):
    tight, wide = policies = pair(first, second)
    for _ in range(2):
        assert limiter.check_all("k", policies).allowed
    before = limiter.peek("k", wide).remaining

    for _ in range(25):
        assert not limiter.check_all("k", policies).allowed

    assert limiter.peek("k", wide).remaining == before == 98
    assert limiter.peek("k", tight).remaining == 0


@pytest.mark.parametrize(("first", "second"), PAIRS)
def test_denied_first_check_stores_nothing(limiter, backend, first, second):
    policies = pair(first, second, tight=1)
    decision = limiter.check_all("fresh", policies, cost=2)
    assert not decision.allowed
    assert stored_state(backend) == 0


@pytest.mark.parametrize(("first", "second"), PAIRS)
def test_order_of_policies_does_not_change_the_outcome(limiter, first, second):
    forward = pair(first, second)
    backward = forward[::-1]
    for key, policies in (("a", forward), ("b", backward)):
        outcomes = [limiter.check_all(key, policies).allowed for _ in range(3)]
        assert outcomes == [True, True, False]
        assert limiter.peek(key, forward[1]).remaining == 98


@pytest.mark.parametrize("algorithm", ALL)
def test_allowed_check_consumes_from_every_policy(limiter, algorithm):
    tight, wide = policies = pair(algorithm, algorithm, tight=5, wide=9)
    decision = limiter.check_all("k", policies, cost=2)
    assert decision.allowed
    assert decision.remaining == 3
    assert limiter.peek("k", tight).remaining == 3
    assert limiter.peek("k", wide).remaining == 7


@pytest.mark.parametrize("algorithm", ALL)
def test_weighted_request_is_all_or_nothing(limiter, algorithm):
    tight, wide = policies = pair(algorithm, algorithm, tight=5, wide=100)
    assert limiter.check_all("k", policies, cost=4).allowed
    denied = limiter.check_all("k", policies, cost=2)
    assert not denied.allowed and denied.remaining == 1
    assert limiter.peek("k", wide).remaining == 96
    assert limiter.check_all("k", policies, cost=1).allowed


def test_second_and_hour_pair_from_the_issue(limiter, clock):
    second = Policy.parse("10/second", name="per-second")
    hour = Policy.parse("15/hour", name="per-hour")
    assert [limiter.check_all("u", [second, hour]).allowed for _ in range(12)] == (
        [True] * 10 + [False] * 2
    )
    clock.advance(2)
    assert [limiter.check_all("u", [second, hour]).allowed for _ in range(6)] == (
        [True] * 5 + [False]
    )
    denied = limiter.check_all("u", [second, hour])
    assert denied.limit == 15 and denied.policy == "per-hour"
    assert denied.retry_after > 60


def test_combined_decision_reports_the_most_restrictive_policy(limiter):
    tight, wide = pair(Algorithm.SLIDING_LOG, Algorithm.FIXED_WINDOW, tight=3, wide=1000)
    decision = limiter.check_all("k", [wide, tight])
    assert decision.policy == "tight"
    assert decision.algorithm is Algorithm.SLIDING_LOG
    assert decision.headers()["X-RateLimit-Limit"] == "3"
    assert decision.headers()["X-RateLimit-Remaining"] == "2"
    assert [part.policy for part in decision.parts] == ["wide", "tight"]
    assert [part.remaining for part in decision.parts] == [999, 2]


def test_retry_after_is_the_longest_wait_among_denying_policies(limiter, clock):
    short = Policy(limit=1, window=10, algorithm=Algorithm.SLIDING_LOG, name="short")
    long = Policy(limit=1, window=500, algorithm=Algorithm.SLIDING_LOG, name="long")
    limiter.check_all("k", [short, long])
    clock.advance(1)
    denied = limiter.check_all("k", [short, long])
    assert not denied.allowed
    assert denied.retry_after == pytest.approx(499)
    assert denied.reset_after == pytest.approx(499)
    assert denied.limit == 1 and denied.policy == "long"
    assert denied.headers()["Retry-After"] == "499"

    clock.advance(10)  # only `long` still blocks; `short` has recovered but stays unconsumed
    denied = limiter.check_all("k", [short, long])
    assert [part.allowed for part in denied.parts] == [True, False]
    assert denied.parts[0].remaining == 1
    assert denied.retry_after == pytest.approx(489)


def test_reset_after_never_undercuts_retry_after(limiter):
    fast = Policy(limit=3, window=2, algorithm=Algorithm.SLIDING_LOG, name="fast")
    slow = Policy(limit=4, window=900, algorithm=Algorithm.SLIDING_LOG, name="slow")
    limiter.check_all("k", [fast, slow], cost=3)
    denied = limiter.check_all("k", [fast, slow], cost=2)
    assert not denied.allowed
    assert denied.policy == "fast" and denied.remaining == 0
    assert denied.retry_after == pytest.approx(900)
    assert denied.reset_after >= denied.retry_after


@pytest.mark.parametrize("algorithm", ALL)
def test_state_is_shared_with_single_policy_calls(limiter, algorithm):
    tight, wide = policies = pair(algorithm, algorithm, tight=3, wide=10)
    limiter.check("k", tight)
    assert limiter.check_all("k", policies).remaining == 1
    limiter.check_all("k", policies)
    assert not limiter.check("k", tight).allowed
    assert limiter.peek("k", wide).remaining == 8

    limiter.reset("k", tight)
    assert limiter.check_all("k", policies).allowed


@pytest.mark.parametrize("algorithm", ALL)
def test_budget_recovers_with_time(limiter, clock, algorithm):
    policies = pair(algorithm, algorithm)
    assert [limiter.check_all("k", policies).allowed for _ in range(3)] == [True, True, False]
    clock.advance(120.5)
    assert limiter.check_all("k", policies).allowed


def test_keys_are_independent(limiter):
    policies = pair(Algorithm.GCRA, Algorithm.TOKEN_BUCKET, tight=1)
    assert limiter.check_all("a", policies).allowed
    assert not limiter.check_all("a", policies).allowed
    assert limiter.check_all("b", policies).allowed


def test_a_single_policy_matches_check(limiter):
    policy = Policy(limit=2, window=60, name="only")
    decision = limiter.check_all("a", [policy])
    expected = limiter.check("b", policy)
    assert (decision.allowed, decision.remaining, decision.limit) == (True, 1, 2)
    assert (expected.allowed, expected.remaining, expected.limit) == (True, 1, 2)
    assert decision.retry_after == 0


def test_accepts_any_iterable_of_policies(limiter):
    policies = pair(Algorithm.GCRA, Algorithm.GCRA)
    assert limiter.check_all("k", iter(policies)).allowed


@pytest.mark.parametrize(
    "policies",
    [
        [],
        [Policy.parse("10/second"), Policy.parse("1000/hour")],
        [Policy(limit=1, window=1, name="a"), Policy(limit=2, window=2, name="a")],
        [Policy(limit=1, window=1, name="a")] * 2,
        [
            Policy(limit=1, window=1, algorithm=Algorithm.GCRA),
            Policy(limit=2, window=2, name="gcra"),
        ],
    ],
    ids=["empty", "unnamed-same-algorithm", "same-name", "repeated", "name-equals-algorithm"],
)
def test_policies_that_cannot_share_a_check_are_rejected(limiter, backend, policies):
    with pytest.raises(ValueError):
        limiter.check_all("k", policies)
    assert stored_state(backend) == 0


def test_unnamed_policies_with_different_algorithms_are_fine(limiter):
    policies = [
        Policy.parse("2/minute;algorithm=gcra"),
        Policy.parse("100/hour;algorithm=sliding_log"),
    ]
    assert limiter.check_all("k", policies).remaining == 1


@pytest.mark.parametrize("cost", [0, -1])
def test_cost_must_be_positive(limiter, cost):
    with pytest.raises(ValueError):
        limiter.check_all("k", pair(Algorithm.GCRA, Algorithm.GCRA), cost=cost)


def test_every_policy_sees_the_same_timestamp():
    class Ticking(ManualClock):
        reads = 0

        def now(self) -> float:
            self.reads += 1
            return super().now()

    clock = Ticking()
    limiter = RateLimiter(MemoryBackend(), clock=clock)
    limiter.check_all("k", pair(Algorithm.GCRA, Algorithm.SLIDING_LOG))
    assert clock.reads == 1


def test_memory_backend_is_atomic_across_threads(clock):
    tight, wide = policies = pair(Algorithm.SLIDING_LOG, Algorithm.TOKEN_BUCKET, tight=40, wide=500)
    limiter = RateLimiter(MemoryBackend(), clock=clock)
    allowed = []

    def worker():
        for _ in range(50):
            allowed.append(limiter.check_all("k", policies).allowed)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(allowed) == 40
    assert limiter.peek("k", wide).remaining == 460


def test_redis_is_atomic_across_clients(real_redis, clock):
    tight, wide = policies = pair(Algorithm.SLIDING_LOG, Algorithm.TOKEN_BUCKET, tight=40, wide=500)
    limiters = [RateLimiter(RedisBackend(real_redis), clock=clock) for _ in range(4)]
    allowed = []

    def worker(limiter):
        for _ in range(50):
            allowed.append(limiter.check_all("k", policies).allowed)

    threads = [threading.Thread(target=worker, args=(limiter,)) for limiter in limiters]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(allowed) == 40
    assert limiters[0].peek("k", tight).remaining == 0
    assert limiters[0].peek("k", wide).remaining == 460


@pytest.mark.parametrize("seed", range(4))
def test_memory_and_redis_make_the_same_decisions(seed):
    rng = random.Random(seed)
    policies = [
        Policy(limit=rng.randint(2, 12), window=rng.choice([5, 30]), algorithm=a, name=a.value)
        for a in rng.sample(ALL, 3)
    ]
    clocks = [ManualClock(start=1_700_000_000.0), ManualClock(start=1_700_000_000.0)]
    memory = RateLimiter(MemoryBackend(), clock=clocks[0])
    redis = RateLimiter(RedisBackend(fakeredis.FakeRedis()), clock=clocks[1])

    for step in range(300):
        cost = rng.choice([1, 1, 1, 2, 5])
        a, b = memory.check_all("k", policies, cost), redis.check_all("k", policies, cost)
        assert (a.allowed, a.remaining, a.limit, a.policy) == (
            b.allowed,
            b.remaining,
            b.limit,
            b.policy,
        ), step
        assert a.retry_after == pytest.approx(b.retry_after, abs=1e-3), step
        elapsed = rng.choice([0, 0, 0.05, 0.4, 3.3])
        for clock in clocks:
            clock.advance(elapsed)
