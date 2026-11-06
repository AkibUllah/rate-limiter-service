"""Redis backend. Every algorithm step is one Lua script, so read-modify-write is atomic even with
many service instances hammering the same key. Floats are returned as strings because Redis
truncates Lua numbers to integers in replies."""

from __future__ import annotations

import secrets

from .base import RawDecision

# Each algorithm is one Lua function with the same signature, so a single-policy script and the
# composite script run exactly the same step. `a` and `b` are that algorithm's two parameters, in
# the order given by the matching method on RedisBackend.
_FIXED_WINDOW_STEP = """
local function fixed_window(key, now, limit, window, cost, dry)
  local start = math.floor(now / window) * window
  local wkey = key .. ':' .. string.format('%.0f', start)
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
end
"""

_SLIDING_LOG_STEP = """
local function sliding_log(key, now, limit, window, cost, dry, nonce)
  -- Entries with score <= now - window have expired. A dry run skips them by rank instead of
  -- deleting them, so the sorted set is left exactly as it was.
  local expired = 0
  if dry then
    expired = redis.call('ZCOUNT', key, '-inf', now - window)
  else
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  end
  local count = redis.call('ZCARD', key) - expired
  local allowed = 0
  if count + cost <= limit then
    allowed = 1
    if not dry then
      for i = 1, cost do redis.call('ZADD', key, now, nonce .. ':' .. i) end
      count = count + cost
    end
  end
  if not dry then redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000) end
  local reset_after = 0
  local oldest = redis.call('ZRANGE', key, expired, expired, 'WITHSCORES')
  if oldest[2] then reset_after = tonumber(oldest[2]) + window - now end
  local retry_after = 0
  if allowed == 0 then retry_after = reset_after end
  return {allowed, tostring(limit - count), tostring(retry_after), tostring(reset_after)}
end
"""

_SLIDING_COUNTER_STEP = """
local function sliding_counter(key, now, limit, window, cost, dry)
  local start = math.floor(now / window) * window
  local data = redis.call('HMGET', key, 'start', 'current', 'previous')
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
    redis.call('HSET', key, 'start', start, 'current', current, 'previous', previous)
    redis.call('PEXPIRE', key, math.ceil(window * 2000) + 1000)
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
end
"""

_TOKEN_BUCKET_STEP = """
local function token_bucket(key, now, capacity, rate, cost, dry)
  local data = redis.call('HMGET', key, 'tokens', 'last')
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
    redis.call('HSET', key, 'tokens', tokens, 'last', now)
    redis.call('PEXPIRE', key, math.ceil(((capacity - tokens) / rate) * 1000) + 1000)
  end
  local retry_after = 0
  if allowed == 0 then retry_after = (cost - tokens) / rate end
  local reset_after = (capacity - tokens) / rate
  return {allowed, tostring(math.floor(tokens + 1e-9)), tostring(retry_after),
          tostring(reset_after)}
end
"""

_GCRA_STEP = """
local function gcra(key, now, period, burst, cost, dry)
  local tat = tonumber(redis.call('GET', key))
  if tat == nil or tat < now then tat = now end
  local new_tat = tat + period * cost
  local allow_at = new_tat - burst * period
  local allowed = 0
  if allow_at <= now + 1e-6 then
    allowed = 1
    if not dry then
      -- tostring keeps 14 significant digits, which rounds an epoch timestamp to 0.1 ms.
      local stored = string.format('%.17g', new_tat)
      redis.call('SET', key, stored, 'PX', math.ceil((new_tat - now) * 1000) + 1000)
      tat = new_tat
    end
  end
  local remaining = math.floor((now + burst * period - tat + 1e-6) / period)
  if remaining < 0 then remaining = 0 end
  local retry_after = 0
  if allowed == 0 then retry_after = allow_at - now end
  local reset_after = tat - now
  if reset_after < 0 then reset_after = 0 end
  return {allowed, tostring(remaining), tostring(retry_after), tostring(reset_after)}
end
"""

_SINGLE_CALL = """
return {name}(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]),
              tonumber(ARGV[4]), ARGV[5] == '1', ARGV[6])
"""

FIXED_WINDOW = _FIXED_WINDOW_STEP + _SINGLE_CALL.format(name="fixed_window")
SLIDING_LOG = _SLIDING_LOG_STEP + _SINGLE_CALL.format(name="sliding_log")
SLIDING_COUNTER = _SLIDING_COUNTER_STEP + _SINGLE_CALL.format(name="sliding_counter")
TOKEN_BUCKET = _TOKEN_BUCKET_STEP + _SINGLE_CALL.format(name="token_bucket")
GCRA = _GCRA_STEP + _SINGLE_CALL.format(name="gcra")


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
        args = [repr(now), limit, repr(window), cost, int(dry_run), nonce]
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
