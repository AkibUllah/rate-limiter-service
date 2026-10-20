"""Process-local backend. One lock guards all state; each call is a few arithmetic operations."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field

from .base import EPSILON, RawDecision


@dataclass
class _Window:
    start: float = 0.0
    count: int = 0


@dataclass
class _SlidingCounter:
    start: float | None = None
    current: int = 0
    previous: int = 0


@dataclass
class _Bucket:
    tokens: float
    last: float


@dataclass
class _Log:
    entries: deque = field(default_factory=deque)


class MemoryBackend:
    blocking = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fixed: dict[str, _Window] = {}
        self._logs: dict[str, _Log] = {}
        self._sliding: dict[str, _SlidingCounter] = {}
        self._buckets: dict[str, _Bucket] = {}
        self._tat: dict[str, float] = {}

    # ------------------------------------------------------------------ algorithms

    def fixed_window(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision:
        start = math.floor(now / window) * window
        with self._lock:
            state = self._fixed.get(key)
            if state is None or state.start != start:
                state = _Window(start=start, count=0)
                self._fixed[key] = state
            allowed = state.count + cost <= limit
            if allowed:
                state.count += cost
            remaining = limit - state.count
        reset_after = start + window - now
        return allowed, remaining, (0.0 if allowed else reset_after), reset_after

    def sliding_log(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision:
        with self._lock:
            log = self._logs.setdefault(key, _Log())
            entries = log.entries
            boundary = now - window
            while entries and entries[0] <= boundary:
                entries.popleft()
            allowed = len(entries) + cost <= limit
            if allowed:
                entries.extend([now] * cost)
            remaining = limit - len(entries)
            oldest = entries[0] if entries else None
        reset_after = (oldest + window - now) if oldest is not None else 0.0
        return allowed, remaining, (0.0 if allowed else reset_after), reset_after

    def sliding_counter(
        self, key: str, limit: int, window: float, cost: int, now: float
    ) -> RawDecision:
        start = math.floor(now / window) * window
        with self._lock:
            state = self._sliding.setdefault(key, _SlidingCounter())
            if state.start is None:
                state.start, state.current, state.previous = start, 0, 0
            elif state.start != start:
                state.previous = state.current if state.start == start - window else 0
                state.current = 0
                state.start = start
            elapsed = (now - start) / window
            estimate = state.previous * (1 - elapsed) + state.current
            allowed = estimate + cost <= limit + EPSILON
            if allowed:
                state.current += cost
                estimate += cost
            previous, current = state.previous, state.current
        reset_after = start + window - now
        retry_after = 0.0
        if not allowed:
            if previous <= 0:
                retry_after = reset_after
            else:
                needed = 1 - (limit - current - cost) / previous
                retry_after = min(max(start + window * needed - now, 0.0), reset_after)
        return allowed, max(limit - estimate, 0.0), retry_after, reset_after

    def token_bucket(
        self, key: str, capacity: int, rate: float, cost: int, now: float
    ) -> RawDecision:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(capacity), last=now)
                self._buckets[key] = bucket
            elapsed = max(now - bucket.last, 0.0)
            bucket.tokens = min(float(capacity), bucket.tokens + elapsed * rate)
            bucket.last = now
            allowed = bucket.tokens + EPSILON >= cost
            if allowed:
                bucket.tokens -= cost
            tokens = bucket.tokens
        retry_after = 0.0 if allowed else (cost - tokens) / rate
        reset_after = (capacity - tokens) / rate
        return allowed, math.floor(tokens + EPSILON), retry_after, reset_after

    def gcra(self, key: str, period: float, burst: int, cost: int, now: float) -> RawDecision:
        with self._lock:
            tat = self._tat.get(key, now)
            if tat < now:
                tat = now
            new_tat = tat + period * cost
            allow_at = new_tat - burst * period
            allowed = allow_at <= now + EPSILON
            if allowed:
                self._tat[key] = new_tat
                tat = new_tat
        remaining = max(math.floor((now + burst * period - tat) / period + EPSILON), 0)
        retry_after = 0.0 if allowed else allow_at - now
        reset_after = max(tat - now, 0.0)
        return allowed, remaining, retry_after, reset_after

    # ------------------------------------------------------------------ housekeeping

    def reset(self, key: str) -> None:
        with self._lock:
            for store in (self._fixed, self._logs, self._sliding, self._buckets, self._tat):
                store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            for store in (self._fixed, self._logs, self._sliding, self._buckets, self._tat):
                store.clear()

    def __len__(self) -> int:
        return sum(
            len(s) for s in (self._fixed, self._logs, self._sliding, self._buckets, self._tat)
        )
