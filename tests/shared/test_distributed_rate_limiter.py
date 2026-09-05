from __future__ import annotations

import math
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.shared import distributed_rate_limiter as limiter_module
from app.shared.distributed_rate_limiter import RedisTokenBucketRateLimiter


class _FakeRedis:
    def __init__(self, results: list[list[int]]) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    async def eval(self, *args):
        self.calls.append(args)
        return self.results.pop(0)


class _FailingFakeRedis:
    """Raises the given exception on every `eval` call -- for proving
    `try_acquire`'s fail-open behavior on a Redis connection/timeout error.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def eval(self, *args):
        raise self._exc


class _SharedBackendFakeRedis:
    """A minimal, real (not canned-response) re-implementation of
    `_TOKEN_BUCKET_SCRIPT`'s own token-bucket logic, backed by one plain
    dict standing in for a real Redis server's keyspace.

    Two independent `RedisTokenBucketRateLimiter` instances constructed
    around the *same* instance of this class model two separate processes
    (e.g. two API replicas) both talking to the one real Redis server they
    actually share in production -- proving state is shared across
    *limiter instances*, not merely reused within one, is exactly what a
    canned-response `_FakeRedis` above cannot demonstrate (it has no actual
    state to share). This is the one test in this file that needs a fake
    faithful enough to prove that.
    """

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, float]] = {}

    async def eval(self, script: str, numkeys: int, key: str, rate, capacity, initial, ttl_ms):
        rate = float(rate)
        capacity = float(capacity)
        initial = float(initial)
        now_ms = time.monotonic() * 1000
        state = self._hashes.get(key)
        if state is None:
            tokens = initial
        else:
            elapsed_seconds = max(0.0, now_ms - state["updated_ms"]) / 1000
            tokens = min(capacity, state["tokens"] + elapsed_seconds * rate)
        allowed = 0
        wait_ms = 0
        if tokens >= 1:
            tokens -= 1
            allowed = 1
        else:
            wait_ms = math.ceil(((1 - tokens) / rate) * 1000)
        self._hashes[key] = {"tokens": tokens, "updated_ms": now_ms}
        return [allowed, wait_ms]


@pytest.mark.asyncio
async def test_distributed_limiter_uses_namespaced_atomic_script() -> None:
    redis = _FakeRedis([[1, 0]])
    limiter = RedisTokenBucketRateLimiter(redis)

    await limiter.acquire("org:abc", 5.0)

    script, key_count, key, rate, capacity, initial, ttl_ms = redis.calls[0]
    assert "redis.call('TIME')" in script
    assert key_count == 1
    assert key == "ekip:rate-limit:org:abc"
    assert (rate, capacity, initial) == (5.0, 5.0, 5.0)
    assert ttl_ms >= 60_000


@pytest.mark.asyncio
async def test_distributed_limiter_waits_then_retries(monkeypatch) -> None:
    redis = _FakeRedis([[0, 250], [1, 0]])
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(limiter_module.asyncio, "sleep", fake_sleep)

    await RedisTokenBucketRateLimiter(redis).acquire("connector:abc", 0.5)

    assert waits == [0.25]
    assert len(redis.calls) == 2


@pytest.mark.asyncio
async def test_distributed_limiter_treats_nonpositive_rate_as_unlimited() -> None:
    redis = _FakeRedis([])

    await RedisTokenBucketRateLimiter(redis).acquire("org:abc", 0)

    assert redis.calls == []



# --- try_acquire (non-blocking, Phase 6.5's API distributed-rate-limiting fix) ---


@pytest.mark.asyncio
async def test_try_acquire_consumes_a_token_when_available() -> None:
    redis = _FakeRedis([[1, 0]])

    allowed = await RedisTokenBucketRateLimiter(redis).try_acquire(
        "user:abc", 5.0, capacity=5.0
    )

    assert allowed is True
    assert len(redis.calls) == 1


@pytest.mark.asyncio
async def test_try_acquire_returns_false_immediately_without_waiting(monkeypatch) -> None:
    """Non-blocking, unlike `acquire`: when the script reports no token
    available, `try_acquire` must return `False` at once -- never sleep and
    retry, unlike `acquire`'s own retry loop. Monkeypatching `asyncio.sleep`
    to fail the test if called is a stronger proof than merely timing the
    call: it fails deterministically instead of relying on wall-clock speed.
    """

    async def fail_if_called(seconds: float) -> None:
        raise AssertionError("try_acquire must not sleep/wait -- it is non-blocking")

    monkeypatch.setattr(limiter_module.asyncio, "sleep", fail_if_called)
    redis = _FakeRedis([[0, 5_000]])  # would ask to wait 5s if this were `acquire`

    allowed = await RedisTokenBucketRateLimiter(redis).try_acquire(
        "user:abc", 1.0, capacity=1.0
    )

    assert allowed is False
    assert len(redis.calls) == 1  # exactly one attempt, no retry


@pytest.mark.asyncio
async def test_try_acquire_shares_state_across_separate_limiter_instances() -> None:
    """The actual bug this fix closes: with an in-process limiter, two
    `TokenBucketRateLimiter` instances (one per API replica) each keep their
    own bucket, so N replicas effectively multiply the real ceiling by N.
    Two `RedisTokenBucketRateLimiter` instances constructed around the same
    shared Redis backend (`_SharedBackendFakeRedis`, standing in for the one
    real Redis server every replica actually talks to) must instead share
    one budget -- a request denied to replica A because replica B already
    spent the budget is exactly what distributed rate limiting means.
    """
    shared_backend = _SharedBackendFakeRedis()
    replica_a = RedisTokenBucketRateLimiter(shared_backend)
    replica_b = RedisTokenBucketRateLimiter(shared_backend)

    # Burst capacity of 2 for "user:abc" -- the first two acquisitions
    # succeed regardless of which replica's limiter instance asks.
    assert await replica_a.try_acquire("user:abc", 1.0, capacity=2.0) is True
    assert await replica_b.try_acquire("user:abc", 1.0, capacity=2.0) is True

    # The budget is now exhausted. A THIRD replica-side instance (modeling
    # a third request landing on yet another replica) must see that same
    # exhausted state, not a fresh bucket of its own -- proving the state
    # lives in the shared backend, not in either limiter object.
    replica_c = RedisTokenBucketRateLimiter(shared_backend)
    assert await replica_c.try_acquire("user:abc", 1.0, capacity=2.0) is False

    # A different key is unaffected -- confirms this isn't a global lockout.
    assert await replica_a.try_acquire("user:xyz", 1.0, capacity=2.0) is True


@pytest.mark.asyncio
async def test_try_acquire_treats_nonpositive_rate_as_unlimited() -> None:
    redis = _FakeRedis([])

    allowed = await RedisTokenBucketRateLimiter(redis).try_acquire("org:abc", 0)

    assert allowed is True
    assert redis.calls == []


@pytest.mark.asyncio
async def test_try_acquire_fails_open_on_redis_connection_error() -> None:
    """Fail-open, not fail-closed: consistent with `app.api.main._lifespan`'s
    own established Redis-unavailability posture (a transient Redis outage
    must degrade only the feature that needs it, never cascade into an
    unrelated endpoint failing outright) -- see `try_acquire`'s own
    docstring. A rate limiter that fails closed here would turn every
    Redis blip into an outage of nearly every human-facing endpoint
    (login, signup, ask, search), which is strictly worse than losing
    distributed throttling for a few seconds.
    """
    redis = _FailingFakeRedis(RedisConnectionError("connection refused"))

    allowed = await RedisTokenBucketRateLimiter(redis).try_acquire(
        "ip:1.2.3.4", 1.0, capacity=1.0
    )

    assert allowed is True


@pytest.mark.asyncio
async def test_try_acquire_fails_open_on_redis_timeout_error() -> None:
    redis = _FailingFakeRedis(RedisTimeoutError("timed out"))

    allowed = await RedisTokenBucketRateLimiter(redis).try_acquire(
        "ip:1.2.3.4", 1.0, capacity=1.0
    )

    assert allowed is True


@pytest.mark.asyncio
async def test_acquire_still_propagates_redis_errors_unchanged() -> None:
    """`acquire` (the existing, unmodified ingestion acquisition mode) must
    NOT gain the new fail-open behavior -- that would be an unrequested
    change to ingestion's own error handling. A Redis error there should
    still surface exactly as it did before this fix.
    """
    redis = _FailingFakeRedis(RedisConnectionError("connection refused"))

    with pytest.raises(RedisConnectionError):
        await RedisTokenBucketRateLimiter(redis).acquire("connector:abc", 1.0)
