from __future__ import annotations

import pytest

from ratelimiter import Algorithm, Policy


def test_parse_simple_rates():
    assert Policy.parse("100/minute") == Policy(100, 60)
    assert Policy.parse("10/second").window == 1
    assert Policy.parse("1000/hour").window == 3600
    assert Policy.parse("5/day").window == 86400
    assert Policy.parse("10/5s") == Policy(10, 5)
    assert Policy.parse("3 / 1.5 min").window == 90


def test_parse_options_and_roundtrip():
    policy = Policy.parse("100/minute;algorithm=token_bucket;burst=20", name="api")
    assert policy.algorithm is Algorithm.TOKEN_BUCKET
    assert policy.burst == 20 and policy.capacity == 20 and policy.name == "api"
    assert policy.rate == pytest.approx(100 / 60)
    assert policy.period == pytest.approx(0.6)
    assert Policy.parse(str(policy)) == Policy(100, 60, Algorithm.TOKEN_BUCKET, burst=20)
    assert Policy.from_dict(policy.to_dict()) == policy


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abc",
        "10/fortnight",
        "10/minute;algorithm=magic",
        "10/minute;burst",
        "10/minute;color=red",
        "0/minute",
    ],
)
def test_parse_rejects_garbage(text):
    with pytest.raises(ValueError):
        Policy.parse(text)


def test_policy_validation():
    with pytest.raises(ValueError):
        Policy(limit=-1, window=10)
    with pytest.raises(ValueError):
        Policy(limit=1, window=0)
    with pytest.raises(ValueError):
        Policy(limit=1, window=1, burst=0)
    assert Policy(limit=1, window=1, algorithm="gcra").algorithm is Algorithm.GCRA
    assert Policy(limit=7, window=1).capacity == 7
