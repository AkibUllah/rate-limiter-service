"""Injectable time source. Backends never call time.time() themselves."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the Unix epoch (wall clock, so Redis-shared state agrees across hosts)."""


class SystemClock:
    def now(self) -> float:
        return time.time()


class ManualClock:
    """Deterministic clock for tests and benchmarks."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += seconds

    def set(self, value: float) -> None:
        self._now = float(value)
