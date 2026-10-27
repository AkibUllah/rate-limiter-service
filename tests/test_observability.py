"""Service: Prometheus metrics and the read-only /v1/inspect endpoint."""

from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from ratelimiter import ManualClock, MemoryBackend, RateLimiter
from ratelimiter.backends.redis import RedisBackend
from ratelimiter.service.app import create_app
from ratelimiter.service.config import Settings
from ratelimiter.service.store import RedisPolicyStore


def make_client(clock, limiter=None, policy_store=None, **settings):
    base = dict(_env_file=None, log_level="WARNING", service_policy=None)
    base.update(settings)
    limiter = limiter or RateLimiter(MemoryBackend(), clock=clock)
    return TestClient(create_app(Settings(**base), limiter=limiter, policy_store=policy_store))


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def client(clock):
    with make_client(clock) as c:
        c.post(
            "/v1/policies",
            json={"name": "otp", "limit": 3, "window_seconds": 60, "algorithm": "sliding_log"},
        )
        yield c


def samples(client, name):
    """{labels-tuple: value} for one metric family, parsed from the /metrics text output."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    out = {}
    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            if sample.name == name:
                out[tuple(sorted(sample.labels.items()))] = sample.value
    return out


def decision_counts(client):
    return {
        (dict(k)["policy"], dict(k)["algorithm"], dict(k)["outcome"]): v
        for k, v in samples(client, "ratelimiter_decisions_total").items()
    }


def test_decision_counters_have_policy_algorithm_and_outcome_labels(client):
    for _ in range(5):
        client.post("/v1/check", json={"key": "phone:1", "policy": "otp"})
    client.post("/v1/check", json={"key": "user:1"})
    client.post(
        "/v1/check",
        json={"key": "t", "inline": {"limit": 1, "window_seconds": 1, "algorithm": "gcra"}},
    )

    assert decision_counts(client) == {
        ("otp", "sliding_log", "allowed"): 3,
        ("otp", "sliding_log", "denied"): 2,
        ("default", "sliding_counter", "allowed"): 1,
        ("inline", "gcra", "allowed"): 1,
    }


def test_backend_latency_histogram_counts_every_check(client):
    for _ in range(4):
        client.post("/v1/check", json={"key": "phone:1", "policy": "otp"})
    counts = samples(client, "ratelimiter_backend_latency_seconds_count")
    assert counts == {(("algorithm", "sliding_log"), ("backend", "memory")): 4}
    total = samples(client, "ratelimiter_backend_latency_seconds_sum")
    assert sum(total.values()) > 0


def test_unknown_policy_and_inspect_are_not_counted(client):
    assert client.post("/v1/check", json={"key": "k", "policy": "nope"}).status_code == 404
    client.get("/v1/inspect", params={"key": "k", "policy": "otp"})
    assert decision_counts(client) == {}


def test_each_app_has_its_own_registry(clock):
    with make_client(clock) as a, make_client(clock) as b:
        a.post("/v1/check", json={"key": "k"})
        assert decision_counts(a) == {("default", "sliding_counter", "allowed"): 1}
        assert decision_counts(b) == {}


def test_metrics_can_be_disabled(clock):
    with make_client(clock, metrics_enabled=False) as c:
        assert c.get("/metrics").status_code == 404
        assert c.post("/v1/check", json={"key": "k"}).json()["allowed"]


def test_metrics_is_exempt_from_the_service_policy(clock):
    with make_client(clock, service_policy="2/minute") as c:
        for _ in range(5):
            assert c.get("/metrics").status_code == 200


def test_inspect_reports_state_without_consuming(client):
    params = {"key": "phone:1", "policy": "otp"}
    fresh = client.get("/v1/inspect", params=params).json()
    assert fresh == {
        "would_allow": True,
        "limit": 3,
        "remaining": 3,
        "retry_after": 0.0,
        "reset_after": 0.0,
        "key": "phone:1",
        "policy": "otp",
        "algorithm": "sliding_log",
        "cost": 1,
    }

    client.post("/v1/check", json=params)
    client.post("/v1/check", json=params)
    for _ in range(10):
        state = client.get("/v1/inspect", params=params).json()
        assert state["would_allow"] and state["remaining"] == 1
        assert state["reset_after"] == pytest.approx(60.0)

    assert client.post("/v1/check", json=params).json()["allowed"]
    exhausted = client.get("/v1/inspect", params=params).json()
    assert not exhausted["would_allow"] and exhausted["remaining"] == 0
    assert exhausted["retry_after"] == pytest.approx(60.0)


def test_inspect_with_cost_and_time_passing(client, clock):
    client.post("/v1/check", json={"key": "k", "policy": "otp", "cost": 2})
    params = {"key": "k", "policy": "otp", "cost": 2}
    assert not client.get("/v1/inspect", params=params).json()["would_allow"]
    clock.advance(60.001)
    assert client.get("/v1/inspect", params=params).json()["would_allow"]


def test_inspect_defaults_to_the_default_policy(client):
    body = client.get("/v1/inspect", params={"key": "user:1"}).json()
    assert body["policy"] == "default" and body["remaining"] == 60


@pytest.mark.parametrize(
    "params",
    [{"policy": "otp"}, {"key": "", "policy": "otp"}, {"key": "k", "cost": 0}],
)
def test_inspect_validation(client, params):
    assert client.get("/v1/inspect", params=params).status_code == 422


def test_inspect_unknown_policy_is_404(client):
    assert client.get("/v1/inspect", params={"key": "k", "policy": "nope"}).status_code == 404


def test_inspect_requires_api_key_when_configured(clock):
    with make_client(clock, api_key="s3cret") as c:
        assert c.get("/v1/inspect", params={"key": "k"}).status_code == 401
        r = c.get("/v1/inspect", params={"key": "k"}, headers={"X-API-Key": "s3cret"})
        assert r.status_code == 200


def test_inspect_sees_state_shared_by_two_redis_instances(clock):
    redis_client = fakeredis.FakeRedis()

    def instance():
        return make_client(
            clock,
            limiter=RateLimiter(RedisBackend(redis_client), clock=clock),
            policy_store=RedisPolicyStore(redis_client, key="rl:policies"),
        )

    with instance() as a, instance() as b:
        a.put(
            "/v1/policies/login",
            json={"name": "login", "limit": 5, "window_seconds": 60, "algorithm": "token_bucket"},
        )
        for _ in range(3):
            a.post("/v1/check", json={"key": "u", "policy": "login"})
        before = redis_client.hgetall("rl:login:u")
        state = b.get("/v1/inspect", params={"key": "u", "policy": "login"}).json()
        assert state["remaining"] == 2 and state["would_allow"]
        assert redis_client.hgetall("rl:login:u") == before
        assert samples(b, "ratelimiter_backend_latency_seconds_count") == {}
