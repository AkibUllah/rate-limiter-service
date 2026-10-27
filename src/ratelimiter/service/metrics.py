"""Prometheus metrics for the service (optional: ``pip install "rate-limiter-service[metrics]"``).

Each app gets its own registry, so several apps in one process (tests, embedded use) never
collide on metric names. With more than one replica, Prometheus scrapes every instance and
aggregates with ``sum by (policy, outcome)``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..decision import Decision

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Histogram,
        generate_latest,
    )
except ImportError:  # pragma: no cover - exercised only without the extra installed
    CollectorRegistry = None  # type: ignore[assignment,misc]

# Backend calls take microseconds in memory and around a millisecond over the network to Redis.
LATENCY_BUCKETS = (0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.1)


def available() -> bool:
    return CollectorRegistry is not None


class Metrics:
    content_type = CONTENT_TYPE_LATEST if available() else "text/plain"

    def __init__(self, backend: str) -> None:
        if not available():
            raise RuntimeError("prometheus-client is not installed")
        self.backend = backend
        self.registry = CollectorRegistry()
        self.decisions = Counter(
            "ratelimiter_decisions_total",
            "Rate limit decisions made by /v1/check.",
            ["policy", "algorithm", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "ratelimiter_backend_latency_seconds",
            "Time spent in the backend for one /v1/check decision.",
            ["backend", "algorithm"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )

    def observe(self, policy: str, check: Callable[[], Decision]) -> Decision:
        """Run ``check`` and record its outcome and duration."""
        started = time.perf_counter()
        decision = check()
        elapsed = time.perf_counter() - started
        algorithm = decision.algorithm.value
        self.latency.labels(self.backend, algorithm).observe(elapsed)
        outcome = "allowed" if decision.allowed else "denied"
        self.decisions.labels(policy, algorithm, outcome).inc()
        return decision

    def render(self) -> bytes:
        return generate_latest(self.registry)
