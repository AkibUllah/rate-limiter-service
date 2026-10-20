from __future__ import annotations

import math
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

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(max(math.ceil(self.reset_after), 0)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(math.ceil(self.retry_after), 1))
        return headers
