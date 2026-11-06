"""Backend protocol.

Each method performs one atomic "try to consume `cost` units" step for a given algorithm and
returns ``(allowed, remaining, retry_after, reset_after)``. Only *allowed* requests consume quota,
so a flood of rejected requests never pushes a client further into the future.

With ``dry_run=True`` the same step is evaluated without writing anything: ``allowed`` says
whether ``cost`` units *would* be accepted and ``remaining`` is the current, unconsumed budget.
This is what ``RateLimiter.peek`` and the service's ``/v1/inspect`` endpoint use.

``now`` is always supplied by the caller (see :mod:`ratelimiter.clock`), which keeps the Redis and
in-memory implementations deterministic under test and consistent across hosts.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

RawDecision = tuple[bool, float, float, float]

EPSILON = 1e-9  # tolerance for float comparisons on token counts and window estimates
TIME_EPSILON = 1e-6  # seconds; epoch timestamps only resolve to about 2.4e-7, so 1e-9 is too tight


class Step(NamedTuple):
    """One policy applied to one storage key: `algorithm` names the backend method to run."""

    algorithm: str
    key: str
    params: tuple[float, float]  # the two policy parameters that method takes after `key`


class Backend(Protocol):
    blocking: bool  # True when calls do network I/O (middleware then offloads to a thread)

    def fixed_window(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision: ...

    def sliding_log(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision: ...

    def sliding_counter(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision: ...

    def token_bucket(
        self, key: str, capacity: int, rate: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision: ...

    def gcra(
        self, key: str, period: float, burst: int, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision: ...

    def reset(self, key: str) -> None: ...
