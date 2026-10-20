from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient

from ratelimiter import ManualClock, MemoryBackend, RateLimiter
from ratelimiter.backends.redis import RedisBackend
from ratelimiter.service.app import create_app
from ratelimiter.service.config import Settings
from ratelimiter.service.store import RedisPolicyStore


def make_client(clock, **settings):
    base = dict(_env_file=None, log_level="WARNING", service_policy=None)
    base.update(settings)
    app = create_app(Settings(**base), limiter=RateLimiter(MemoryBackend(), clock=clock))
    return TestClient(app)


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def client(clock):
    with make_client(clock) as c:
        yield c


def test_health_and_algorithms(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["backend"] == "memory" and health["policies"] == 1
    algorithms = {a["name"] for a in client.get("/v1/algorithms").json()}
    assert algorithms == {"token_bucket", "fixed_window", "sliding_log", "sliding_counter", "gcra"}


def test_policy_crud(client):
    r = client.post(
        "/v1/policies",
        json={"name": "login", "limit": 5, "window_seconds": 60, "algorithm": "sliding_log"},
    )
    assert r.status_code == 201
    assert r.json()["rate_per_second"] == pytest.approx(5 / 60)
    assert (
        client.post(
            "/v1/policies", json={"name": "login", "limit": 1, "window_seconds": 1}
        ).status_code
        == 409
    )

    names = [p["name"] for p in client.get("/v1/policies").json()]
    assert names == ["default", "login"]

    r = client.put("/v1/policies/login", json={"name": "login", "limit": 10, "window_seconds": 60})
    assert r.status_code == 200 and r.json()["limit"] == 10
    assert (
        client.put(
            "/v1/policies/login", json={"name": "other", "limit": 1, "window_seconds": 1}
        ).status_code
        == 400
    )
    assert client.get("/v1/policies/login").json()["algorithm"] == "sliding_counter"
    assert client.get("/v1/policies/missing").status_code == 404
    assert client.delete("/v1/policies/login").status_code == 200
    assert client.delete("/v1/policies/login").status_code == 404
    assert client.delete("/v1/policies/default").status_code == 400


def test_check_with_named_policy(client, clock):
    client.post(
        "/v1/policies",
        json={"name": "login", "limit": 2, "window_seconds": 60, "algorithm": "fixed_window"},
    )
    results = [
        client.post("/v1/check", json={"key": "ip:1.2.3.4", "policy": "login"}).json()
        for _ in range(3)
    ]
    assert [r["allowed"] for r in results] == [True, True, False]
    assert [r["remaining"] for r in results] == [1, 0, 0]
    assert results[-1]["retry_after"] > 0 and results[-1]["policy"] == "login"
    assert results[-1]["algorithm"] == "fixed_window"

    r = client.post("/v1/reset", json={"key": "ip:1.2.3.4", "policy": "login"})
    assert r.status_code == 200
    assert client.post("/v1/check", json={"key": "ip:1.2.3.4", "policy": "login"}).json()["allowed"]


def test_check_falls_back_to_default_policy(client):
    r = client.post("/v1/check", json={"key": "anon"})
    assert r.status_code == 200
    body = r.json()
    assert body["policy"] == "default" and body["limit"] == 60 and body["allowed"]


def test_check_with_inline_policy_and_cost(client):
    inline = {"limit": 10, "window_seconds": 60, "algorithm": "token_bucket", "burst": 10}
    r = client.post("/v1/check", json={"key": "job:7", "inline": inline, "cost": 8})
    assert r.json()["allowed"] and r.json()["remaining"] == 2 and r.json()["policy"] == "inline"
    r = client.post("/v1/check", json={"key": "job:7", "inline": inline, "cost": 3})
    assert r.json()["allowed"] is False


def test_check_validation_and_unknown_policy(client):
    assert client.post("/v1/check", json={"key": "k", "policy": "ghost"}).status_code == 404
    both = {"key": "k", "policy": "default", "inline": {"limit": 1, "window_seconds": 1}}
    assert client.post("/v1/check", json=both).status_code == 422
    assert client.post("/v1/check", json={"key": "", "policy": "default"}).status_code == 422
    assert client.post("/v1/check", json={"key": "k", "cost": 0}).status_code == 422
    assert (
        client.post(
            "/v1/policies", json={"name": "bad name!", "limit": 1, "window_seconds": 1}
        ).status_code
        == 422
    )


def test_api_key_protection(clock):
    with make_client(clock, api_key="s3cret") as c:
        assert c.get("/health").status_code == 200
        assert c.post("/v1/check", json={"key": "k"}).status_code == 401
        assert (
            c.post("/v1/check", json={"key": "k"}, headers={"X-API-Key": "s3cret"}).status_code
            == 200
        )


def test_service_protects_itself_with_middleware(clock):
    with make_client(clock, service_policy="2/minute;algorithm=fixed_window") as c:
        assert c.get("/health").status_code == 200  # exempt
        assert c.get("/v1/algorithms").status_code == 200
        r = c.get("/v1/algorithms")
        assert r.status_code == 200 and r.headers["X-RateLimit-Remaining"] == "0"
        assert c.get("/v1/algorithms").status_code == 429


def test_redis_backed_service_shares_policies_and_state(clock):
    redis = fakeredis.FakeRedis()
    limiter = RateLimiter(RedisBackend(redis), clock=clock)
    store = RedisPolicyStore(redis)
    settings = Settings(_env_file=None, log_level="WARNING", service_policy=None)
    app_a = create_app(settings, limiter=limiter, policy_store=store)
    app_b = create_app(
        settings,
        limiter=RateLimiter(RedisBackend(redis), clock=clock),
        policy_store=RedisPolicyStore(redis),
    )
    with TestClient(app_a) as a, TestClient(app_b) as b:
        assert a.get("/health").json()["backend"] == "redis"
        a.post(
            "/v1/policies",
            json={"name": "shared", "limit": 1, "window_seconds": 60, "algorithm": "gcra"},
        )
        assert b.get("/v1/policies/shared").status_code == 200
        assert a.post("/v1/check", json={"key": "k", "policy": "shared"}).json()["allowed"] is True
        assert b.post("/v1/check", json={"key": "k", "policy": "shared"}).json()["allowed"] is False
