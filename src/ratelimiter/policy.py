"""Policies: how many requests, over what window, enforced by which algorithm."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

_UNITS: dict[str, float] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d*\.?\d*)\s*([a-zA-Z]+)\s*$")


class Algorithm(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_LOG = "sliding_log"
    SLIDING_COUNTER = "sliding_counter"
    GCRA = "gcra"

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_DESCRIPTIONS = {
    Algorithm.TOKEN_BUCKET: "Bucket of `burst` tokens refilled at limit/window per second; "
    "allows short bursts, smooths sustained traffic.",
    Algorithm.FIXED_WINDOW: "Counter per aligned window; cheapest, but allows up to 2x limit "
    "across a window boundary.",
    Algorithm.SLIDING_LOG: "Exact: stores every request timestamp; memory grows with the limit.",
    Algorithm.SLIDING_COUNTER: "Weighted sum of current and previous window counters; "
    "near-exact with O(1) memory.",
    Algorithm.GCRA: "Generic cell rate algorithm (leaky bucket as a meter); one timestamp per key, "
    "precise spacing with a burst allowance.",
}


@dataclass(frozen=True)
class Policy:
    limit: int
    window: float  # seconds
    algorithm: Algorithm = Algorithm.SLIDING_COUNTER
    burst: int | None = None  # token bucket capacity / GCRA burst; defaults to `limit`
    name: str | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        if self.window <= 0:
            raise ValueError("window must be positive")
        if self.burst is not None and self.burst <= 0:
            raise ValueError("burst must be positive")
        if not isinstance(self.algorithm, Algorithm):
            object.__setattr__(self, "algorithm", Algorithm(self.algorithm))

    @property
    def rate(self) -> float:
        """Sustained requests per second."""
        return self.limit / self.window

    @property
    def period(self) -> float:
        """Seconds between requests at the sustained rate (GCRA emission interval)."""
        return self.window / self.limit

    @property
    def capacity(self) -> int:
        return self.burst if self.burst is not None else self.limit

    def with_name(self, name: str) -> Policy:
        return replace(self, name=name)

    @classmethod
    def parse(cls, text: str, name: str | None = None) -> Policy:
        """Parse ``"100/minute"``, ``"10/5s"``, ``"1000/hour;algorithm=gcra;burst=50"``."""
        parts = [p.strip() for p in text.split(";") if p.strip()]
        if not parts:
            raise ValueError("empty policy string")
        match = _RATE_RE.match(parts[0])
        if not match:
            raise ValueError(f"invalid rate {parts[0]!r}; expected e.g. '100/minute' or '10/5s'")
        limit = int(match.group(1))
        multiplier = float(match.group(2)) if match.group(2) else 1.0
        unit = match.group(3).lower()
        if unit not in _UNITS:
            raise ValueError(f"unknown time unit {unit!r}")
        window = multiplier * _UNITS[unit]

        algorithm = Algorithm.SLIDING_COUNTER
        burst: int | None = None
        for option in parts[1:]:
            if "=" not in option:
                raise ValueError(f"invalid option {option!r}; expected key=value")
            key, value = (s.strip().lower() for s in option.split("=", 1))
            if key == "algorithm":
                try:
                    algorithm = Algorithm(value)
                except ValueError as exc:
                    raise ValueError(f"unknown algorithm {value!r}") from exc
            elif key == "burst":
                burst = int(value)
            else:
                raise ValueError(f"unknown option {key!r}")
        return cls(limit=limit, window=window, algorithm=algorithm, burst=burst, name=name)

    def __str__(self) -> str:
        window = int(self.window) if float(self.window).is_integer() else self.window
        text = f"{self.limit}/{window}s;algorithm={self.algorithm.value}"
        if self.burst is not None:
            text += f";burst={self.burst}"
        return text

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "limit": self.limit,
            "window_seconds": self.window,
            "algorithm": self.algorithm.value,
            "burst": self.burst,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        return cls(
            limit=int(data["limit"]),
            window=float(data["window_seconds"]),
            algorithm=Algorithm(data.get("algorithm", Algorithm.SLIDING_COUNTER.value)),
            burst=data.get("burst"),
            name=data.get("name"),
        )
