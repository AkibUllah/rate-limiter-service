"""RateLimiter: binds a backend and a clock, dispatches on the policy's algorithm."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

from .backends.base import Backend, RawDecision, Step
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

    def check_all(self, key: str, policies: Iterable[Policy], cost: int = 1) -> Decision:
        """Consume `cost` units from every policy, or from none of them.

        All policies are evaluated atomically against one timestamp. The returned decision is
        allowed only if each policy allows; see :meth:`Decision.combine` for how the other fields
        are merged, and ``Decision.parts`` for the per-policy results. On a denial, a part that
        would have passed reports its budget as it stands, since nothing was consumed.

        Each policy needs its own state, so policies must be distinct: see
        :meth:`validate_policies`. State is shared with :meth:`check`, :meth:`peek` and
        :meth:`reset` for the same policy.
        """
        policies = self.validate_policies(policies)
        if cost <= 0:
            raise ValueError("cost must be positive")
        steps = [self._step(key, policy) for policy in policies]
        raws = self.backend.evaluate_all(steps, cost, self.clock.now())
        decision = Decision.combine(
            [self._decision(key, policy, raw) for policy, raw in zip(policies, raws, strict=True)]
        )
        if not decision.allowed:
            denied_by = [p.policy or p.algorithm.value for p in decision.parts if not p.allowed]
            log.debug(
                "rate limited key=%s policies=%s retry_after=%.3f",
                key,
                denied_by,
                decision.retry_after,
            )
        return decision

    def validate_policies(self, policies: Iterable[Policy]) -> list[Policy]:
        """Reject a policy list that cannot be checked together, and return it as a list.

        State is stored per policy name, or per algorithm for an unnamed policy, so two unnamed
        policies with the same algorithm (or two with the same name) would share one budget.
        """
        policies = list(policies)
        if not policies:
            raise ValueError("at least one policy is required")
        scopes = [self.storage_key("", policy) for policy in policies]
        if len(set(scopes)) != len(scopes):
            raise ValueError(
                "policies in one check must have distinct state; give each one a unique name"
            )
        return policies

    def peek(self, key: str, policy: Policy, cost: int = 1) -> Decision:
        """Report the state of `key` without consuming anything.

        ``allowed`` tells whether a request of `cost` units would be accepted right now,
        ``remaining`` is the budget left before that request and ``retry_after`` is how long
        until it would be accepted. Calling this any number of times leaves the state unchanged.
        """
        return self._evaluate(key, policy, cost, dry_run=True)

    def _step(self, key: str, policy: Policy) -> Step:
        algorithm = policy.algorithm
        if algorithm is Algorithm.TOKEN_BUCKET:
            params = (policy.capacity, policy.rate)
        elif algorithm is Algorithm.GCRA:
            params = (policy.period, policy.capacity)
        else:
            params = (policy.limit, policy.window)
        return Step(algorithm.value, self.storage_key(key, policy), params)

    @staticmethod
    def _decision(key: str, policy: Policy, raw: RawDecision) -> Decision:
        allowed, remaining, retry_after, reset_after = raw
        algorithm = policy.algorithm
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
            policy=policy.name,
        )

    def _evaluate(self, key: str, policy: Policy, cost: int, dry_run: bool) -> Decision:
        if cost <= 0:
            raise ValueError("cost must be positive")
        step = self._step(key, policy)
        run = getattr(self.backend, step.algorithm)
        raw = run(step.key, *step.params, cost, self.clock.now(), dry_run)
        return self._decision(key, policy, raw)

    def reset(self, key: str, policy: Policy) -> None:
        self.backend.reset(self.storage_key(key, policy))
