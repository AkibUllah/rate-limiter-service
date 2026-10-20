"""Behavioural tests for every algorithm, run against the memory and the (fake) Redis backend."""

from __future__ import annotations

import pytest

from ratelimiter import Algorithm, Policy

ALL = list(Algorithm)


def burst(limiter, key, policy, n):
    return [limiter.check(key, policy) for _ in range(n)]


@pytest.mark.parametrize("algorithm", ALL)
def test_allows_limit_then_denies(limiter, algorithm):
    policy = Policy(limit=5, window=10, algorithm=algorithm)
    decisions = burst(limiter, "u1", policy, 6)
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert [d.remaining for d in decisions] == [4, 3, 2, 1, 0, 0]
    assert decisions[-1].retry_after > 0
    assert decisions[-1].headers()["Retry-After"].isdigit()
    assert decisions[0].headers()["X-RateLimit-Limit"] == "5"


@pytest.mark.parametrize("algorithm", ALL)
def test_keys_are_independent(limiter, algorithm):
    policy = Policy(limit=2, window=10, algorithm=algorithm)
    burst(limiter, "a", policy, 2)
    assert not limiter.check("a", policy).allowed
    assert limiter.check("b", policy).allowed


@pytest.mark.parametrize("algorithm", ALL)
def test_denied_requests_do_not_consume_quota(limiter, clock, algorithm):
    policy = Policy(limit=3, window=30, algorithm=algorithm)
    burst(limiter, "k", policy, 3)
    for _ in range(50):  # hammering while limited must not push the reset further out
        assert not limiter.check("k", policy).allowed
    clock.advance(30.001)
    assert limiter.check("k", policy).allowed


@pytest.mark.parametrize("algorithm", ALL)
def test_two_windows_later_everything_is_available_again(limiter, clock, algorithm):
    policy = Policy(limit=4, window=10, algorithm=algorithm)
    burst(limiter, "k", policy, 4)
    clock.advance(20.5)  # sliding counters still weight the previous window one window later
    decisions = burst(limiter, "k", policy, 4)
    assert all(d.allowed for d in decisions)


@pytest.mark.parametrize("algorithm", ALL)
def test_reset_clears_state(limiter, algorithm):
    policy = Policy(limit=1, window=60, algorithm=algorithm, name="named")
    assert limiter.check("k", policy).allowed
    assert not limiter.check("k", policy).allowed
    limiter.reset("k", policy)
    assert limiter.check("k", policy).allowed


@pytest.mark.parametrize("algorithm", ALL)
def test_cost_consumes_multiple_units(limiter, algorithm):
    policy = Policy(limit=10, window=60, algorithm=algorithm)
    assert limiter.check("k", policy, cost=7).allowed
    assert not limiter.check("k", policy, cost=4).allowed
    assert limiter.check("k", policy, cost=3).allowed
    with pytest.raises(ValueError):
        limiter.check("k", policy, cost=0)


@pytest.mark.parametrize("algorithm", ALL)
def test_decision_metadata(limiter, algorithm):
    policy = Policy(limit=5, window=10, algorithm=algorithm)
    d = limiter.check("meta", policy)
    assert d.key == "meta" and d.algorithm is algorithm and d.limit == 5
    assert d.retry_after == 0 and d.reset_after >= 0


# ---------------------------------------------------------------------- algorithm specifics


def test_fixed_window_resets_at_aligned_boundary(limiter, clock):
    policy = Policy(limit=2, window=10, algorithm=Algorithm.FIXED_WINDOW)
    clock.set(1_700_000_008.0)  # 8s into the window
    burst(limiter, "k", policy, 2)
    denied = limiter.check("k", policy)
    assert not denied.allowed and denied.retry_after == pytest.approx(2.0)
    clock.advance(2.0)  # new aligned window -> the classic 2x burst at the boundary
    assert all(d.allowed for d in burst(limiter, "k", policy, 2))


def test_sliding_log_is_exact_across_the_boundary(limiter, clock):
    policy = Policy(limit=3, window=10, algorithm=Algorithm.SLIDING_LOG)
    clock.set(1_700_000_008.0)
    burst(limiter, "k", policy, 3)
    clock.advance(2.0)
    denied = limiter.check("k", policy)  # entries at t=8 are still inside (8, 18]
    assert not denied.allowed
    assert denied.retry_after == pytest.approx(8.0)
    clock.advance(8.0)  # exactly 10s after the entries -> expired
    assert limiter.check("k", policy).allowed


def test_sliding_counter_weights_previous_window(limiter, clock):
    policy = Policy(limit=10, window=10, algorithm=Algorithm.SLIDING_COUNTER)
    clock.set(1_700_000_000.0)
    burst(limiter, "k", policy, 10)
    clock.advance(10.0)  # start of next window: previous=10 weighted at 100%
    assert not limiter.check("k", policy).allowed
    clock.advance(5.0)  # halfway: estimate = 10 * 0.5 = 5 -> 5 more allowed
    decisions = burst(limiter, "k", policy, 6)
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert 0 < decisions[-1].retry_after <= 5.0


def test_sliding_counter_retry_after_predicts_next_slot(limiter, clock):
    policy = Policy(limit=10, window=10, algorithm=Algorithm.SLIDING_COUNTER)
    clock.set(1_700_000_000.0)
    burst(limiter, "k", policy, 10)
    clock.advance(10.0)
    denied = limiter.check("k", policy)
    clock.advance(denied.retry_after + 1e-6)
    assert limiter.check("k", policy).allowed


def test_token_bucket_refills_continuously(limiter, clock):
    policy = Policy(
        limit=60, window=60, algorithm=Algorithm.TOKEN_BUCKET, burst=3
    )  # 1 token/s, burst 3
    decisions = burst(limiter, "k", policy, 4)
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after == pytest.approx(1.0)
    clock.advance(1.0)
    assert limiter.check("k", policy).allowed
    assert not limiter.check("k", policy).allowed
    clock.advance(2.5)
    d = burst(limiter, "k", policy, 3)
    assert [x.allowed for x in d] == [True, True, False]  # 2.5 tokens -> 2 whole requests


def test_token_bucket_never_exceeds_capacity(limiter, clock):
    policy = Policy(limit=10, window=10, algorithm=Algorithm.TOKEN_BUCKET, burst=5)
    limiter.check("k", policy)
    clock.advance(1_000)
    decisions = burst(limiter, "k", policy, 6)
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert decisions[0].remaining == 4


def test_gcra_enforces_spacing_after_burst(limiter, clock):
    policy = Policy(limit=10, window=10, algorithm=Algorithm.GCRA, burst=2)  # period 1s, burst 2
    assert [d.allowed for d in burst(limiter, "k", policy, 3)] == [True, True, False]
    denied = limiter.check("k", policy)
    assert denied.retry_after == pytest.approx(1.0)
    clock.advance(0.5)
    assert not limiter.check("k", policy).allowed
    clock.advance(0.5)
    assert limiter.check("k", policy).allowed
    # Steady state: exactly one request per period.
    for _ in range(5):
        clock.advance(1.0)
        assert limiter.check("k", policy).allowed
        assert not limiter.check("k", policy).allowed


def test_gcra_default_burst_equals_limit(limiter):
    policy = Policy(limit=4, window=4, algorithm=Algorithm.GCRA)
    decisions = burst(limiter, "k", policy, 5)
    assert [d.allowed for d in decisions] == [True] * 4 + [False]
    assert decisions[-1].reset_after == pytest.approx(4.0)


def test_sliding_log_memory_is_bounded_by_pruning(memory_limiter, clock):
    policy = Policy(limit=5, window=1, algorithm=Algorithm.SLIDING_LOG)
    for _ in range(100):
        clock.advance(0.5)
        memory_limiter.check("k", policy)
    entries = memory_limiter.backend._logs[memory_limiter.storage_key("k", policy)].entries
    assert len(entries) <= 5


def test_storage_key_is_scoped_by_policy_name_or_algorithm(memory_limiter):
    named = Policy(limit=1, window=1, name="login")
    anon = Policy(limit=1, window=1, algorithm=Algorithm.GCRA)
    assert memory_limiter.storage_key("k", named) == "rl:login:k"
    assert memory_limiter.storage_key("k", anon) == "rl:gcra:k"
    # Different policies on the same key do not interfere.
    assert memory_limiter.check("k", named).allowed
    assert memory_limiter.check("k", anon).allowed
