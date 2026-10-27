"""FastAPI application factory for the rate limiting service."""

from __future__ import annotations

import logging
import secrets
from functools import partial

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security, status
from fastapi.security import APIKeyHeader

from .. import __version__
from ..backends.memory import MemoryBackend
from ..clock import Clock
from ..limiter import RateLimiter
from ..middleware import RateLimitMiddleware
from ..policy import Algorithm, Policy
from . import metrics as prom
from .config import Settings, get_settings
from .schemas import (
    AlgorithmOut,
    CheckIn,
    CheckOut,
    HealthOut,
    InspectOut,
    MessageOut,
    PolicyIn,
    PolicyOut,
    ResetIn,
)
from .store import MemoryPolicyStore, PolicyStore, RedisPolicyStore

log = logging.getLogger("ratelimiter.service")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(request: Request, api_key: str | None = Security(_api_key_header)) -> None:
    expected = request.app.state.settings.api_key
    if not expected:
        return
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def _store(request: Request) -> PolicyStore:
    return request.app.state.policy_store


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


def create_app(
    settings: Settings | None = None,
    limiter: RateLimiter | None = None,
    policy_store: PolicyStore | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if limiter is None:
        if settings.redis_url:
            import redis

            from ..backends.redis import RedisBackend

            client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
            limiter = RateLimiter(RedisBackend(client), clock=clock, key_prefix=settings.key_prefix)
            policy_store = policy_store or RedisPolicyStore(
                client, key=f"{settings.key_prefix}policies"
            )
            log.info("using redis backend")
        else:
            limiter = RateLimiter(MemoryBackend(), clock=clock, key_prefix=settings.key_prefix)
            log.info("using in-memory backend")
    policy_store = policy_store or MemoryPolicyStore()

    default_policy = Policy.parse(settings.default_policy, name="default")
    if policy_store.get("default") is None:
        policy_store.put(default_policy)

    app = FastAPI(
        title="Rate Limiter Service",
        version=__version__,
        description="Check requests against named or inline rate limit policies.",
    )
    app.state.settings = settings
    app.state.limiter = limiter
    app.state.policy_store = policy_store
    app.state.metrics = None
    if settings.metrics_enabled:
        if prom.available():
            app.state.metrics = prom.Metrics(backend="redis" if limiter.blocking else "memory")
        else:
            log.warning("METRICS_ENABLED is set but prometheus-client is not installed")

    if settings.service_policy:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=limiter,
            policy=Policy.parse(settings.service_policy, name="__service__"),
            exempt_paths=("/health", "/metrics", "/docs", "/openapi.json"),
        )

    protected = [Depends(require_api_key)]

    @app.get("/health", response_model=HealthOut, tags=["meta"])
    def health(request: Request) -> HealthOut:
        return HealthOut(
            status="ok",
            version=__version__,
            backend="redis" if limiter.blocking else "memory",
            policies=len(_store(request).list()),
        )

    if app.state.metrics is not None:

        @app.get("/metrics", tags=["meta"], response_class=Response)
        def metrics() -> Response:
            return Response(app.state.metrics.render(), media_type=app.state.metrics.content_type)

    @app.get("/v1/algorithms", response_model=list[AlgorithmOut], tags=["meta"])
    def algorithms() -> list[AlgorithmOut]:
        return [AlgorithmOut(name=a, description=a.description) for a in Algorithm]

    @app.post("/v1/check", response_model=CheckOut, tags=["limits"], dependencies=protected)
    def check(payload: CheckIn, request: Request) -> CheckOut:
        store = _store(request)
        if payload.inline is not None:
            policy = payload.inline.to_policy(name=None)
            policy_name = "inline"
        else:
            name = payload.policy or "default"
            policy = store.get(name)
            if policy is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy {name!r} not found")
            policy_name = name
        run = partial(_limiter(request).check, payload.key, policy, cost=payload.cost)
        metrics = app.state.metrics
        decision = metrics.observe(policy_name, run) if metrics is not None else run()
        return CheckOut(
            allowed=decision.allowed,
            limit=decision.limit,
            remaining=decision.remaining,
            retry_after=round(decision.retry_after, 6),
            reset_after=round(decision.reset_after, 6),
            key=payload.key,
            policy=policy_name,
            algorithm=decision.algorithm,
        )

    @app.get("/v1/inspect", response_model=InspectOut, tags=["limits"], dependencies=protected)
    def inspect(
        request: Request,
        key: str = Query(min_length=1, max_length=256),
        policy: str = Query(default="default", description="Name of a stored policy"),
        cost: int = Query(default=1, gt=0, le=10_000),
    ) -> InspectOut:
        """Current state of `key` under a stored policy. Read-only: consumes nothing."""
        stored = _store(request).get(policy)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy {policy!r} not found")
        decision = _limiter(request).peek(key, stored, cost=cost)
        return InspectOut(
            would_allow=decision.allowed,
            limit=decision.limit,
            remaining=decision.remaining,
            retry_after=round(decision.retry_after, 6),
            reset_after=round(decision.reset_after, 6),
            key=key,
            policy=policy,
            algorithm=decision.algorithm,
            cost=cost,
        )

    @app.post("/v1/reset", response_model=MessageOut, tags=["limits"], dependencies=protected)
    def reset(payload: ResetIn, request: Request) -> MessageOut:
        policy = _store(request).get(payload.policy)
        if policy is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy {payload.policy!r} not found")
        _limiter(request).reset(payload.key, policy)
        return MessageOut(message=f"State cleared for key {payload.key!r} under {payload.policy!r}")

    @app.get(
        "/v1/policies", response_model=list[PolicyOut], tags=["policies"], dependencies=protected
    )
    def list_policies(request: Request) -> list[PolicyOut]:
        return [PolicyOut.from_policy(p) for p in _store(request).list()]

    @app.post(
        "/v1/policies",
        response_model=PolicyOut,
        status_code=status.HTTP_201_CREATED,
        tags=["policies"],
        dependencies=protected,
    )
    def create_policy(payload: PolicyIn, request: Request) -> PolicyOut:
        store = _store(request)
        if store.get(payload.name) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Policy {payload.name!r} already exists")
        policy = payload.to_policy(name=payload.name)
        store.put(policy)
        return PolicyOut.from_policy(policy)

    @app.get(
        "/v1/policies/{name}", response_model=PolicyOut, tags=["policies"], dependencies=protected
    )
    def get_policy(name: str, request: Request) -> PolicyOut:
        policy = _store(request).get(name)
        if policy is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy {name!r} not found")
        return PolicyOut.from_policy(policy)

    @app.put(
        "/v1/policies/{name}", response_model=PolicyOut, tags=["policies"], dependencies=protected
    )
    def upsert_policy(name: str, payload: PolicyIn, request: Request) -> PolicyOut:
        if payload.name != name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body name must match the path")
        policy = payload.to_policy(name=name)
        _store(request).put(policy)
        return PolicyOut.from_policy(policy)

    @app.delete(
        "/v1/policies/{name}", response_model=MessageOut, tags=["policies"], dependencies=protected
    )
    def delete_policy(name: str, request: Request) -> MessageOut:
        if name == "default":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The default policy cannot be deleted")
        if not _store(request).delete(name):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Policy {name!r} not found")
        return MessageOut(message=f"Policy {name!r} deleted")

    return app
