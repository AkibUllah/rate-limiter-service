from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ratelimiter import Algorithm, ManualClock, MemoryBackend, Policy, RateLimiter
from ratelimiter.middleware import RateLimitMiddleware, header_key


def build(policy, clock, **kwargs):
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"pong": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    limiter = RateLimiter(MemoryBackend(), clock=clock)
    app.add_middleware(RateLimitMiddleware, limiter=limiter, policy=policy, **kwargs)
    return TestClient(app), limiter


@pytest.fixture
def clock():
    return ManualClock()


def test_headers_and_429(clock):
    client, _ = build(Policy(limit=2, window=60, algorithm=Algorithm.SLIDING_LOG), clock)
    r1 = client.get("/ping")
    assert r1.status_code == 200
    assert r1.headers["X-RateLimit-Limit"] == "2"
    assert r1.headers["X-RateLimit-Remaining"] == "1"
    assert "X-RateLimit-Reset" in r1.headers
    client.get("/ping")
    r3 = client.get("/ping")
    assert r3.status_code == 429
    assert r3.headers["X-RateLimit-Remaining"] == "0"
    assert r3.headers["Retry-After"] == "60"
    body = r3.json()
    assert body["detail"] == "Rate limit exceeded" and body["retry_after"] == 60.0

    clock.advance(60.001)
    assert client.get("/ping").status_code == 200


def test_exempt_paths_bypass_the_limiter(clock):
    client, _ = build(Policy(limit=1, window=60), clock)
    client.get("/ping")
    assert client.get("/ping").status_code == 429
    r = client.get("/health")
    assert r.status_code == 200 and "X-RateLimit-Limit" not in r.headers


def test_key_by_api_key_header_with_ip_fallback(clock):
    client, _ = build(Policy(limit=1, window=60), clock, key_func=header_key("X-API-Key"))
    assert client.get("/ping", headers={"X-API-Key": "alice"}).status_code == 200
    assert client.get("/ping", headers={"X-API-Key": "alice"}).status_code == 429
    assert client.get("/ping", headers={"X-API-Key": "bob"}).status_code == 200
    assert client.get("/ping").status_code == 200  # falls back to the client IP
    assert client.get("/ping").status_code == 429


def test_forwarded_for_is_used_for_ip_keys(clock):
    client, _ = build(Policy(limit=1, window=60), clock)
    assert (
        client.get("/ping", headers={"X-Forwarded-For": "10.0.0.1, 192.168.0.1"}).status_code == 200
    )
    assert client.get("/ping", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
    assert client.get("/ping", headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


def test_policy_can_depend_on_the_request(clock):
    strict = Policy(limit=1, window=60, name="strict")
    loose = Policy(limit=100, window=60, name="loose")

    def choose(scope):
        if scope["path"].startswith("/ping"):
            return strict
        return None  # unlimited

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {}

    @app.get("/free")
    def free():
        return {}

    app.add_middleware(RateLimitMiddleware, limiter=RateLimiter(clock=clock), policy=choose)
    client = TestClient(app)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    for _ in range(5):
        r = client.get("/free")
        assert r.status_code == 200 and "X-RateLimit-Limit" not in r.headers
    assert loose.limit == 100


def test_cost_func_and_on_limited_hook(clock):
    seen = []

    async def on_limited(scope, decision):
        seen.append((scope["path"], decision.retry_after))

    client, _ = build(
        Policy(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET),
        clock,
        cost_func=lambda scope: 6,
        on_limited=on_limited,
    )
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    assert seen and seen[0][0] == "/ping" and seen[0][1] > 0
