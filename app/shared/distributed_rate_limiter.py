"""Redis-backed token buckets for limits shared across worker replicas and,
as of Phase 6.5's distributed rate-limiting fix, across API replicas too
(`app.api.rate_limit`).
"""

from __future__ import annotations

import asyncio
import math
from typing import Protocol

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.shared.config.logging import get_logger

logger = get_logger(__name__)


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
    """Token bucket whose state is shared by every Redis client -- across
    worker replicas (`acquire`, blocking) and, since Phase 6.5's
    distributed-rate-limiting fix, across API replicas too (`try_acquire`,
    non-blocking; see its own docstring).
    """

    def __init__(self, redis: object, *, namespace: str = "ekip:rate-limit") -> None:
        self._redis = redis
        self._namespace = namespace.rstrip(":")

    async def _eval_bucket(
        self, key: str, rate: float, capacity: float, initial_tokens: float
    ) -> tuple[int, int]:
        """Run the atomic token-bucket script once for `key` and return
        `(allowed, wait_ms)` -- the one place either acquisition mode talks
        to Redis, so the script/argument wiring exists exactly once. Caller
        decides what "allowed" means (retry-until-allowed for `acquire`,
        return-immediately for `try_acquire`), resolves `initial_tokens`
        (see `acquire`'s own comment -- it differs depending on whether the
        caller passed an explicit `capacity`), and whether a Redis error
        itself should be caught.
        """
        # Idle buckets need not live forever. Retain at least a minute and at
        # least two full refill windows so normal bursts preserve continuity.
        ttl_ms = math.ceil(max(60.0, (capacity / rate) * 2) * 1000)
        redis_key = f"{self._namespace}:{key}"
        result = await self._redis.eval(
            _TOKEN_BUCKET_SCRIPT,
            1,
            redis_key,
            rate,
            capacity,
            initial_tokens,
            ttl_ms,
        )
        return int(result[0]), int(result[1])

    async def acquire(
        self, key: str, rate: float, *, capacity: float | None = None
    ) -> None:
        """Block until one token is available for `key`, then consume it.
        Unchanged behavior (including on a Redis error, which still
        propagates uncaught) -- this is `app.ingestion.workers.tasks`'s
        existing background-job acquisition mode, out of scope for the
        API-layer distributed rate-limiting fix this module's `try_acquire`
        was added for.
        """
        if rate <= 0:
            return
        effective_capacity = max(rate, 1.0) if capacity is None else capacity
        initial_tokens = rate if capacity is None else effective_capacity

        while True:
            allowed, wait_ms = await self._eval_bucket(
                key, rate, effective_capacity, initial_tokens
            )
            if allowed:
                return
            await asyncio.sleep(max(wait_ms, 1) / 1000)

    async def try_acquire(
        self, key: str, rate: float, *, capacity: float | None = None
    ) -> bool:
        """Non-blocking: consume one token for `key` if immediately
        available, else return `False` at once -- never waits, unlike
        `acquire`. This is what `app.api.rate_limit` needs: an HTTP request
        path must return `429` promptly, never hang a client waiting for a
        token to accrue (the same reasoning `app.shared.rate_limiter.
        TokenBucketRateLimiter.try_acquire`'s own docstring already gives
        for the in-process limiter this replaces there).

        Same `rate <= 0` == "no limit" convention as `acquire`.

        Fail-open on a Redis connection/timeout error: returns `True`
        (allow the request) rather than raising, logging a warning instead.
        Consistent with this project's existing Redis-unavailability
        posture -- `app.api.main._lifespan`'s own docstring establishes
        that a transient Redis outage must degrade only the one feature
        that needs it, not cascade into unrelated endpoints failing
        outright (that incident was one connector-sync endpoint; rate
        limiting sits in front of nearly every human-facing endpoint, so
        failing closed here would be strictly worse -- an API-wide outage
        every time Redis blips, including login). This does not change
        `acquire`'s own behavior (still propagates a Redis error uncaught,
        matching ingestion's existing, unmodified expectations).
        """
        if rate <= 0:
            return True
        effective_capacity = max(rate, 1.0) if capacity is None else capacity
        initial_tokens = rate if capacity is None else effective_capacity

        try:
            allowed, _wait_ms = await self._eval_bucket(
                key, rate, effective_capacity, initial_tokens
            )
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.warning("rate_limiter_redis_unavailable", key=key, error=str(exc))
            return True

        return bool(allowed)
