"""Named policy storage: in-process dict, or a Redis hash shared by all instances."""

from __future__ import annotations

import json
import threading
from typing import Protocol

from ..policy import Policy


class PolicyStore(Protocol):
    def get(self, name: str) -> Policy | None: ...

    def put(self, policy: Policy) -> None: ...

    def delete(self, name: str) -> bool: ...

    def list(self) -> list[Policy]: ...


class MemoryPolicyStore:
    def __init__(self) -> None:
        self._policies: dict[str, Policy] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Policy | None:
        with self._lock:
            return self._policies.get(name)

    def put(self, policy: Policy) -> None:
        if not policy.name:
            raise ValueError("stored policies need a name")
        with self._lock:
            self._policies[policy.name] = policy

    def delete(self, name: str) -> bool:
        with self._lock:
            return self._policies.pop(name, None) is not None

    def list(self) -> list[Policy]:
        with self._lock:
            return sorted(self._policies.values(), key=lambda p: p.name or "")


class RedisPolicyStore:
    def __init__(self, client, key: str = "rl:policies") -> None:
        self._redis = client
        self._key = key

    def get(self, name: str) -> Policy | None:
        raw = self._redis.hget(self._key, name)
        return Policy.from_dict(json.loads(raw)) if raw else None

    def put(self, policy: Policy) -> None:
        if not policy.name:
            raise ValueError("stored policies need a name")
        self._redis.hset(self._key, policy.name, json.dumps(policy.to_dict()))

    def delete(self, name: str) -> bool:
        return bool(self._redis.hdel(self._key, name))

    def list(self) -> list[Policy]:
        values = self._redis.hgetall(self._key).values()
        return sorted((Policy.from_dict(json.loads(v)) for v in values), key=lambda p: p.name or "")
