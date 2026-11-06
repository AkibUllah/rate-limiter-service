from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .policy import Algorithm


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # seconds until the request would be accepted (0 when allowed)
    reset_after: float  # seconds until the limiter state for this key is back to "full"
    key: str
    algorithm: Algorithm
    policy: str | None = None  # name of the policy that produced this decision, if it has one
    parts: tuple[Decision, ...] = ()  # per-policy decisions behind a composite check

    @classmethod
    def combine(cls, parts: Sequence[Decision]) -> Decision:
        """Merge per-policy decisions into the one a caller should act on.

        ``allowed`` needs every policy to allow, ``remaining`` is the smallest budget and
        ``retry_after`` the longest wait. ``limit``, ``reset_after``, ``algorithm`` and ``policy``
        come from the most restrictive policy, so the rate limit headers describe one real limit
        rather than a mix: a denying policy first, then the least remaining, then the longest
        wait. ``reset_after`` is never shorter than ``retry_after``.
        """
        binding = min(parts, key=lambda d: (d.allowed, d.remaining, -d.retry_after, -d.reset_after))
        retry_after = max(d.retry_after for d in parts)
        return cls(
            allowed=all(d.allowed for d in parts),
            limit=binding.limit,
            remaining=min(d.remaining for d in parts),
            retry_after=retry_after,
            reset_after=max(binding.reset_after, retry_after),
            key=binding.key,
            algorithm=binding.algorithm,
            policy=binding.policy,
            parts=tuple(parts),
        )

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(max(math.ceil(self.reset_after), 0)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(math.ceil(self.retry_after), 1))
        return headers
