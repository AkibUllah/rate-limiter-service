from __future__ import annotations

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ratelimiter import Algorithm, ManualClock, MemoryBackend, Policy, RateLimiter
from ratelimiter.backends.redis import RedisBackend
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


def named_pair():
    return [
        Policy(limit=3, window=1, algorithm=Algorithm.SLIDING_LOG, name="second"),
        Policy(limit=5, window=3600, algorithm=Algorithm.SLIDING_LOG, name="hour"),
    ]


def test_list_of_policies_reports_the_most_restrictive_limit(clock):
    client, _ = build(named_pair(), clock)
    first = client.get("/ping")
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "3"
    assert first.headers["X-RateLimit-Remaining"] == "2"
    assert first.headers["X-RateLimit-Reset"] == "1"

    client.get("/ping")
    client.get("/ping")
    denied = client.get("/ping")
    assert denied.status_code == 429
    assert denied.headers["X-RateLimit-Limit"] == "3"
    assert denied.headers["Retry-After"] == "1"


def test_denied_request_consumes_nothing_from_the_wider_policy(clock):
    second, hour = policies = named_pair()
    client, limiter = build(policies, clock)
    for _ in range(3):
        client.get("/ping")
    for _ in range(10):
        assert client.get("/ping").status_code == 429
    assert limiter.peek("testclient", hour).remaining == 2

    clock.advance(1.001)
    assert [client.get("/ping").status_code for _ in range(3)] == [200, 200, 429]
    denied = client.get("/ping")
    assert denied.headers["X-RateLimit-Limit"] == "5"
    assert int(denied.headers["Retry-After"]) >= 3598


def test_policy_callable_may_return_a_list_or_nothing(clock):
    def choose(scope):
        return None if scope.get("query_string") == b"open=1" else named_pair()

    client, _ = build(choose, clock)
    assert client.get("/ping").headers["X-RateLimit-Limit"] == "3"
    assert "X-RateLimit-Limit" not in client.get("/ping?open=1").headers


def test_policy_callable_returning_an_empty_list_skips_limiting(clock):
    client, _ = build(lambda scope: [], clock)
    response = client.get("/ping")
    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers


def test_on_limited_hook_sees_the_per_policy_results(clock):
    seen = []

    async def on_limited(scope, decision):
        seen.append([(part.policy, part.allowed) for part in decision.parts])

    client, _ = build(named_pair(), clock, on_limited=on_limited)
    for _ in range(4):
        client.get("/ping")
    assert seen == [[("second", False), ("hour", True)]]


def test_list_of_policies_runs_in_a_thread_on_redis(clock):
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"pong": True}

    limiter = RateLimiter(RedisBackend(fakeredis.FakeRedis()), clock=clock)
    app.add_middleware(RateLimitMiddleware, limiter=limiter, policy=named_pair())
    client = TestClient(app)
    assert [client.get("/ping").status_code for _ in range(4)] == [200, 200, 200, 429]


@pytest.mark.parametrize(
    "policies",
    [[], [Policy.parse("10/second"), Policy.parse("1000/hour")]],
    ids=["empty", "shared-state"],
)
def test_a_bad_policy_list_fails_at_startup(clock, policies):
    limiter = RateLimiter(MemoryBackend(), clock=clock)
    with pytest.raises(ValueError):
        RateLimitMiddleware(FastAPI(), limiter=limiter, policy=policies)
