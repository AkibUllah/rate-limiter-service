"""`Decision.combine`: merging per-policy results into one."""

from __future__ import annotations

import pytest

from ratelimiter import Algorithm, Decision


def part(name, allowed=True, limit=10, remaining=5, retry_after=0.0, reset_after=1.0):
    return Decision(
        allowed=allowed,
        limit=limit,
        remaining=remaining,
        retry_after=retry_after,
        reset_after=reset_after,
        key="k",
        algorithm=Algorithm.GCRA,
        policy=name,
    )


def test_all_allowed_reports_the_policy_with_the_least_budget():
    second = part("second", limit=10, remaining=9, reset_after=1)
    hour = part("hour", limit=1000, remaining=999, reset_after=3600)
    combined = Decision.combine([hour, second])
    assert combined.allowed
    assert (combined.limit, combined.remaining, combined.reset_after) == (10, 9, 1)
    assert combined.policy == "second"
    assert combined.parts == (hour, second)


def test_one_denial_denies_the_whole_check():
    combined = Decision.combine(
        [part("a"), part("b", allowed=False, remaining=0, retry_after=2.0, reset_after=2.0)]
    )
    assert not combined.allowed
    assert combined.policy == "b" and combined.retry_after == 2.0


def test_ties_on_remaining_go_to_the_longest_wait():
    short = part("short", allowed=False, remaining=0, retry_after=1.0, reset_after=1.0)
    long = part(
        "long", allowed=False, remaining=0, retry_after=3000.0, reset_after=3000.0, limit=99
    )
    combined = Decision.combine([short, long])
    assert combined.policy == "long" and combined.limit == 99


def test_a_denying_policy_wins_over_an_allowing_one_on_equal_budget():
    allowing = part("allowing", remaining=1, limit=3)
    denying = part("denying", allowed=False, remaining=1, limit=4, retry_after=0.5)
    assert Decision.combine([allowing, denying]).policy == "denying"


def test_headers_describe_the_combined_result():
    combined = Decision.combine(
        [
            part("a", remaining=7),
            part("b", allowed=False, limit=3, remaining=0, retry_after=9.2, reset_after=4.0),
        ]
    )
    assert combined.headers() == {
        "X-RateLimit-Limit": "3",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "10",
        "Retry-After": "10",
    }


def test_combining_nothing_is_an_error():
    with pytest.raises(ValueError):
        Decision.combine([])
