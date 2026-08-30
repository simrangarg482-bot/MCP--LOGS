from __future__ import annotations

import pytest

from app.shared import distributed_rate_limiter as limiter_module
from app.shared.distributed_rate_limiter import RedisTokenBucketRateLimiter


class _FakeRedis:
    def __init__(self, results: list[list[int]]) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    async def eval(self, *args):
        self.calls.append(args)
        return self.results.pop(0)


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
