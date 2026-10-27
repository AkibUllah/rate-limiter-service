"""RateLimiter: binds a backend and a clock, dispatches on the policy's algorithm."""

from __future__ import annotations

import logging
import math

from .backends.base import Backend
from .backends.memory import MemoryBackend
from .clock import Clock, SystemClock
from .decision import Decision
from .policy import Algorithm, Policy

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(
        self,
        backend: Backend | None = None,
        clock: Clock | None = None,
        key_prefix: str = "rl:",
    ) -> None:
        self.backend: Backend = backend if backend is not None else MemoryBackend()
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.key_prefix = key_prefix

    @property
    def blocking(self) -> bool:
        return bool(getattr(self.backend, "blocking", False))

    def storage_key(self, key: str, policy: Policy) -> str:
        scope = policy.name or policy.algorithm.value
        return f"{self.key_prefix}{scope}:{key}"

    def check(self, key: str, policy: Policy, cost: int = 1) -> Decision:
        """Try to consume `cost` units for `key` under `policy`."""
        decision = self._evaluate(key, policy, cost, dry_run=False)
        if not decision.allowed:
            log.debug(
                "rate limited key=%s policy=%s retry_after=%.3f", key, policy, decision.retry_after
            )
        return decision

    def peek(self, key: str, policy: Policy, cost: int = 1) -> Decision:
        """Report the state of `key` without consuming anything.

        ``allowed`` tells whether a request of `cost` units would be accepted right now,
        ``remaining`` is the budget left before that request and ``retry_after`` is how long
        until it would be accepted. Calling this any number of times leaves the state unchanged.
        """
        return self._evaluate(key, policy, cost, dry_run=True)

    def _evaluate(self, key: str, policy: Policy, cost: int, dry_run: bool) -> Decision:
        if cost <= 0:
            raise ValueError("cost must be positive")
        now = self.clock.now()
        skey = self.storage_key(key, policy)
        algorithm = policy.algorithm

        if algorithm is Algorithm.FIXED_WINDOW:
            raw = self.backend.fixed_window(skey, policy.limit, policy.window, cost, now, dry_run)
        elif algorithm is Algorithm.SLIDING_LOG:
            raw = self.backend.sliding_log(skey, policy.limit, policy.window, cost, now, dry_run)
        elif algorithm is Algorithm.SLIDING_COUNTER:
            raw = self.backend.sliding_counter(
                skey, policy.limit, policy.window, cost, now, dry_run
            )
        elif algorithm is Algorithm.TOKEN_BUCKET:
            raw = self.backend.token_bucket(skey, policy.capacity, policy.rate, cost, now, dry_run)
        elif algorithm is Algorithm.GCRA:
            raw = self.backend.gcra(skey, policy.period, policy.capacity, cost, now, dry_run)
        else:  # pragma: no cover - enum is exhaustive
            raise ValueError(f"unsupported algorithm {algorithm!r}")

        allowed, remaining, retry_after, reset_after = raw
        return Decision(
            allowed=allowed,
            limit=policy.capacity
            if algorithm in (Algorithm.TOKEN_BUCKET, Algorithm.GCRA)
            else policy.limit,
            remaining=max(int(math.floor(remaining + 1e-9)), 0),
            retry_after=max(retry_after, 0.0),
            reset_after=max(reset_after, 0.0),
            key=key,
            algorithm=algorithm,
        )

    def reset(self, key: str, policy: Policy) -> None:
        self.backend.reset(self.storage_key(key, policy))
