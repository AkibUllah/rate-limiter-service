"""Distributed rate limiting with swappable algorithms and backends.

Quick use::

    from ratelimiter import RateLimiter, Policy

    limiter = RateLimiter()                       # in-memory backend
    policy = Policy.parse("100/minute;algorithm=token_bucket;burst=20")
    decision = limiter.check("user:42", policy)
    if not decision.allowed:
        ...  # decision.retry_after seconds
"""

from .backends.memory import MemoryBackend
from .clock import Clock, ManualClock, SystemClock
from .decision import Decision
from .limiter import RateLimiter
from .middleware import RateLimitMiddleware, client_ip_key, header_key
from .policy import Algorithm, Policy

__version__ = "0.1.0"

__all__ = [
    "Algorithm",
    "Clock",
    "Decision",
    "ManualClock",
    "MemoryBackend",
    "Policy",
    "RateLimitMiddleware",
    "RateLimiter",
    "SystemClock",
    "client_ip_key",
    "header_key",
    "__version__",
]
