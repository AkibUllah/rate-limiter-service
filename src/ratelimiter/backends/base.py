"""Backend protocol.

Each method performs one atomic "try to consume `cost` units" step for a given algorithm and
returns ``(allowed, remaining, retry_after, reset_after)``. Only *allowed* requests consume quota,
so a flood of rejected requests never pushes a client further into the future.

``now`` is always supplied by the caller (see :mod:`ratelimiter.clock`), which keeps the Redis and
in-memory implementations deterministic under test and consistent across hosts.
"""

from __future__ import annotations

from typing import Protocol

RawDecision = tuple[bool, float, float, float]

EPSILON = 1e-9  # tolerance for float comparisons on accumulated timestamps


class Backend(Protocol):
    blocking: bool  # True when calls do network I/O (middleware then offloads to a thread)

    def fixed_window(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision: ...

    def sliding_log(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision: ...

    def sliding_counter(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision: ...

    def token_bucket(
        self, key: str, capacity: int, rate: float, cost: int, now: float
    ) -> RawDecision: ...

    def gcra(self, key: str, period: float, burst: int, cost: int, now: float) -> RawDecision: ...

    def reset(self, key: str) -> None: ...
