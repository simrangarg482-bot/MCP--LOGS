"""Redis-backed token buckets for limits shared across worker replicas."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol


class AsyncRateLimiter(Protocol):
    async def acquire(
        self, key: str, rate: float, *, capacity: float | None = None
    ) -> None: ...


# Redis server time, not worker wall-clock time, keeps refill calculations
# consistent across machines. The read/refill/consume operation is atomic.
_TOKEN_BUCKET_SCRIPT = """
local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local initial = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms')
local tokens = tonumber(state[1])
local updated_ms = tonumber(state[2])
if tokens == nil or updated_ms == nil then
    tokens = initial
    updated_ms = now_ms
else
    local elapsed_seconds = math.max(0, now_ms - updated_ms) / 1000
    tokens = math.min(capacity, tokens + (elapsed_seconds * rate))
end
local allowed = 0
local wait_ms = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    wait_ms = math.ceil(((1 - tokens) / rate) * 1000)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {allowed, wait_ms}
"""


class RedisTokenBucketRateLimiter:
    """Blocking token bucket whose state is shared by every Redis client."""

    def __init__(self, redis: object, *, namespace: str = "ekip:rate-limit") -> None:
        self._redis = redis
        self._namespace = namespace.rstrip(":")

    async def acquire(
        self, key: str, rate: float, *, capacity: float | None = None
    ) -> None:
        if rate <= 0:
            return
        effective_capacity = max(rate, 1.0) if capacity is None else capacity
        initial_tokens = rate if capacity is None else effective_capacity
        # Idle buckets need not live forever. Retain at least a minute and at
        # least two full refill windows so normal bursts preserve continuity.
        ttl_ms = math.ceil(max(60.0, (effective_capacity / rate) * 2) * 1000)
        redis_key = f"{self._namespace}:{key}"

        while True:
            result = await self._redis.eval(
                _TOKEN_BUCKET_SCRIPT,
                1,
                redis_key,
                rate,
                effective_capacity,
                initial_tokens,
                ttl_ms,
            )
            allowed, wait_ms = (int(result[0]), int(result[1]))
            if allowed:
                return
            await asyncio.sleep(max(wait_ms, 1) / 1000)
