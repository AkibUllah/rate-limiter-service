"""Redis backend. Every algorithm step is one Lua script, so read-modify-write is atomic even with
many service instances hammering the same key. Floats are returned as strings because Redis
truncates Lua numbers to integers in replies."""

from __future__ import annotations

import secrets

from .base import RawDecision

FIXED_WINDOW = """
local now = tonumber(ARGV[1]); local limit = tonumber(ARGV[2])
local window = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local dry = ARGV[5] == '1'
local start = math.floor(now / window) * window
local wkey = KEYS[1] .. ':' .. string.format('%.0f', start)
local count = tonumber(redis.call('GET', wkey) or '0')
local allowed = 0
if count + cost <= limit then
  allowed = 1
  if not dry then
    count = count + cost
    redis.call('SET', wkey, count, 'PX', math.ceil(window * 1000) + 1000)
  end
end
local reset_after = start + window - now
local retry_after = 0
if allowed == 0 then retry_after = reset_after end
return {allowed, tostring(limit - count), tostring(retry_after), tostring(reset_after)}
"""

SLIDING_LOG = """
local now = tonumber(ARGV[1]); local limit = tonumber(ARGV[2])
local window = tonumber(ARGV[3]); local cost = tonumber(ARGV[4]); local nonce = ARGV[5]
local dry = ARGV[6] == '1'
-- Entries with score <= now - window have expired. A dry run skips them by rank instead of
-- deleting them, so the sorted set is left exactly as it was.
local expired = 0
if dry then
  expired = redis.call('ZCOUNT', KEYS[1], '-inf', now - window)
else
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
end
local count = redis.call('ZCARD', KEYS[1]) - expired
local allowed = 0
if count + cost <= limit then
  allowed = 1
  if not dry then
    for i = 1, cost do redis.call('ZADD', KEYS[1], now, nonce .. ':' .. i) end
    count = count + cost
  end
end
if not dry then redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000) + 1000) end
local reset_after = 0
local oldest = redis.call('ZRANGE', KEYS[1], expired, expired, 'WITHSCORES')
if oldest[2] then reset_after = tonumber(oldest[2]) + window - now end
local retry_after = 0
if allowed == 0 then retry_after = reset_after end
return {allowed, tostring(limit - count), tostring(retry_after), tostring(reset_after)}
"""

SLIDING_COUNTER = """
local now = tonumber(ARGV[1]); local limit = tonumber(ARGV[2])
local window = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local dry = ARGV[5] == '1'
local start = math.floor(now / window) * window
local data = redis.call('HMGET', KEYS[1], 'start', 'current', 'previous')
local stored = tonumber(data[1]); local current = tonumber(data[2] or '0')
local previous = tonumber(data[3] or '0')
if stored == nil then
  current = 0; previous = 0
elseif stored ~= start then
  if stored == start - window then previous = current else previous = 0 end
  current = 0
end
local elapsed = (now - start) / window
local estimate = previous * (1 - elapsed) + current
local allowed = 0
if estimate + cost <= limit + 1e-9 then
  allowed = 1
  if not dry then
    current = current + cost
    estimate = estimate + cost
  end
end
if not dry then
  redis.call('HSET', KEYS[1], 'start', start, 'current', current, 'previous', previous)
  redis.call('PEXPIRE', KEYS[1], math.ceil(window * 2000) + 1000)
end
local reset_after = start + window - now
local retry_after = 0
if allowed == 0 then
  if previous <= 0 then
    retry_after = reset_after
  else
    local needed = 1 - (limit - current - cost) / previous
    local t = start + window * needed - now
    if t < 0 then t = 0 end
    if t > reset_after then t = reset_after end
    retry_after = t
  end
end
local remaining = limit - estimate
if remaining < 0 then remaining = 0 end
return {allowed, tostring(remaining), tostring(retry_after), tostring(reset_after)}
"""

TOKEN_BUCKET = """
local now = tonumber(ARGV[1]); local capacity = tonumber(ARGV[2])
local rate = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local dry = ARGV[5] == '1'
local data = redis.call('HMGET', KEYS[1], 'tokens', 'last')
local tokens = tonumber(data[1]); local last = tonumber(data[2])
if tokens == nil then tokens = capacity; last = now end
local elapsed = now - last
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
if tokens + 1e-9 >= cost then
  allowed = 1
  if not dry then tokens = tokens - cost end
end
if not dry then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'last', now)
  redis.call('PEXPIRE', KEYS[1], math.ceil(((capacity - tokens) / rate) * 1000) + 1000)
end
local retry_after = 0
if allowed == 0 then retry_after = (cost - tokens) / rate end
local reset_after = (capacity - tokens) / rate
return {allowed, tostring(math.floor(tokens + 1e-9)), tostring(retry_after), tostring(reset_after)}
"""

GCRA = """
local now = tonumber(ARGV[1]); local period = tonumber(ARGV[2])
local burst = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local dry = ARGV[5] == '1'
local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil or tat < now then tat = now end
local new_tat = tat + period * cost
local allow_at = new_tat - burst * period
local allowed = 0
if allow_at <= now + 1e-9 then
  allowed = 1
  if not dry then
    redis.call('SET', KEYS[1], tostring(new_tat), 'PX', math.ceil((new_tat - now) * 1000) + 1000)
    tat = new_tat
  end
end
local remaining = math.floor((now + burst * period - tat) / period + 1e-9)
if remaining < 0 then remaining = 0 end
local retry_after = 0
if allowed == 0 then retry_after = allow_at - now end
local reset_after = tat - now
if reset_after < 0 then reset_after = 0 end
return {allowed, tostring(remaining), tostring(retry_after), tostring(reset_after)}
"""


class RedisBackend:
    blocking = True

    def __init__(self, client) -> None:
        self._redis = client
        self._fixed = client.register_script(FIXED_WINDOW)
        self._log = client.register_script(SLIDING_LOG)
        self._counter = client.register_script(SLIDING_COUNTER)
        self._bucket = client.register_script(TOKEN_BUCKET)
        self._gcra = client.register_script(GCRA)

    @staticmethod
    def _decode(reply) -> RawDecision:
        allowed, remaining, retry_after, reset_after = reply
        return bool(int(allowed)), float(remaining), float(retry_after), float(reset_after)

    def fixed_window(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision:
        args = [repr(now), limit, repr(window), cost, int(dry_run)]
        return self._decode(self._fixed(keys=[key], args=args))

    def sliding_log(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision:
        nonce = secrets.token_hex(8)
        args = [repr(now), limit, repr(window), cost, nonce, int(dry_run)]
        return self._decode(self._log(keys=[key], args=args))

    def sliding_counter(
        self, key: str, limit: int, window: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision:
        args = [repr(now), limit, repr(window), cost, int(dry_run)]
        return self._decode(self._counter(keys=[key], args=args))

    def token_bucket(
        self, key: str, capacity: int, rate: float, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision:
        args = [repr(now), capacity, repr(rate), cost, int(dry_run)]
        return self._decode(self._bucket(keys=[key], args=args))

    def gcra(
        self, key: str, period: float, burst: int, cost: int, now: float, dry_run: bool = False
    ) -> RawDecision:
        args = [repr(now), repr(period), burst, cost, int(dry_run)]
        return self._decode(self._gcra(keys=[key], args=args))

    def reset(self, key: str) -> None:
        # Fixed-window keys carry a window suffix; SCAN them out rather than guessing.
        self._redis.delete(key)
        for suffixed in self._redis.scan_iter(match=f"{key}:*", count=100):
            self._redis.delete(suffixed)


def redis_backend_from_url(url: str) -> RedisBackend:
    import redis

    return RedisBackend(redis.Redis.from_url(url, decode_responses=False))
