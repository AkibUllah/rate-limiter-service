"""Pure ASGI middleware for Starlette / FastAPI.

* adds ``X-RateLimit-Limit``, ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset`` to every response
* answers ``429`` with ``Retry-After`` and a JSON body when the policy is exhausted
* identifies clients by IP by default (``X-Forwarded-For`` aware); plug in your own ``key_func``
  to limit per API key, per user, per tenant, ...
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .decision import Decision
from .limiter import RateLimiter
from .policy import Policy

KeyFunc = Callable[[Scope], str | None]
PolicyFunc = Callable[[Scope], Policy | None]


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def client_ip_key(scope: Scope) -> str | None:
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def header_key(header_name: str, fallback: KeyFunc | None = client_ip_key) -> KeyFunc:
    """Key by a request header (e.g. ``X-API-Key``); fall back to the IP when absent."""
    encoded = header_name.lower().encode("latin-1")

    def key_func(scope: Scope) -> str | None:
        value = _header(scope, encoded)
        if value:
            return f"{header_name.lower()}:{value}"
        return fallback(scope) if fallback else None

    return key_func


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        policy: Policy | PolicyFunc,
        key_func: KeyFunc = client_ip_key,
        exempt_paths: Iterable[str] = ("/health",),
        cost_func: Callable[[Scope], int] | None = None,
        on_limited: Callable[[Scope, Decision], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self._policy = policy
        self.key_func = key_func
        self.exempt_paths = tuple(exempt_paths)
        self.cost_func = cost_func
        self.on_limited = on_limited

    def policy_for(self, scope: Scope) -> Policy | None:
        if isinstance(self._policy, Policy):
            return self._policy
        return self._policy(scope)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        policy = self.policy_for(scope)
        key = self.key_func(scope) if policy is not None else None
        if policy is None or key is None:
            await self.app(scope, receive, send)
            return

        cost = self.cost_func(scope) if self.cost_func else 1
        if self.limiter.blocking:
            decision = await run_in_threadpool(self.limiter.check, key, policy, cost)
        else:
            decision = self.limiter.check(key, policy, cost)

        if not decision.allowed:
            if self.on_limited is not None:
                await self.on_limited(scope, decision)
            await self._reject(send, decision)
            return

        headers = decision.headers()

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                raw.extend(
                    (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
                )
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(send: Send, decision: Decision) -> None:
        body = json.dumps(
            {
                "detail": "Rate limit exceeded",
                "retry_after": round(decision.retry_after, 3),
                "limit": decision.limit,
            }
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        headers.extend(
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in decision.headers().items()
        )
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})
