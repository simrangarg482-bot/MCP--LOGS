"""`core.auth.sso_transaction`: single-use, expiring, opaque."""

from __future__ import annotations

import json
import time

import pytest

from app.core.auth import sso_transaction as txn

KW = dict(org_slug="acme", redirect_uri="http://localhost:5173/o/acme/callback", state="st", nonce="no", code_verifier="ver")


class FakeRedis:
    """Just enough of redis.asyncio.Redis (SIMULATED -- not a real Redis server):
    `set(key, value, ex=)` and an atomic `getdel(key)` returning bytes like the real client."""

    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.broken = False

    async def set(self, key, value, ex=None):
        if self.broken:
            raise ConnectionError("redis down")
        self.kv[key] = value.encode()
        self.ttls[key] = ex

    async def getdel(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return self.kv.pop(key, None)


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def store(redis):
    return txn.RedisSsoTransactionStore(redis)


@pytest.mark.asyncio
async def test_round_trip_then_single_use(store) -> None:
    handle = await txn.create_transaction(store, ttl_seconds=600, **KW)

    first = await txn.consume_transaction(store, handle)
    second = await txn.consume_transaction(store, handle)

    assert first is not None and first.state == "st" and first.nonce == "no"
    assert first.code_verifier == "ver" and first.org_slug == "acme"
    assert second is None  # consumed


@pytest.mark.asyncio
async def test_handle_is_random_and_not_the_storage_key(store, redis) -> None:
    handles = {await txn.create_transaction(store, ttl_seconds=60, **KW) for _ in range(20)}
    assert len(handles) == 20 and all(len(h) >= 40 for h in handles)
    for key in redis.kv:
        assert key.startswith("ekip:sso:txn:") and not any(h in key for h in handles)
    assert all(ttl == 60 for ttl in redis.ttls.values())  # Redis-side expiry is set


@pytest.mark.asyncio
async def test_expiry_is_enforced_even_if_redis_kept_the_key(store, monkeypatch) -> None:
    handle = await txn.create_transaction(store, ttl_seconds=60, **KW)
    real = time.time
    monkeypatch.setattr(txn.time, "time", lambda: real() + 61)

    assert await txn.consume_transaction(store, handle) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "nope", "x" * 129, "\x00", "a b"])
async def test_unknown_handles_find_nothing(store, bad) -> None:
    await txn.create_transaction(store, ttl_seconds=60, **KW)
    assert await txn.consume_transaction(store, bad) is None


@pytest.mark.asyncio
async def test_corrupt_record_is_rejected_not_crashed(store, redis) -> None:
    handle = await txn.create_transaction(store, ttl_seconds=60, **KW)
    (key,) = redis.kv
    for garbage in (b"not json", json.dumps({"org_slug": "a"}).encode(), b"[]"):
        redis.kv[key] = garbage
        assert await txn.consume_transaction(store, handle) is None
        redis.kv[key] = garbage  # restore for the next round (consume deletes)


@pytest.mark.asyncio
async def test_redis_failure_is_reported_as_unavailable(store, redis) -> None:
    redis.broken = True
    with pytest.raises(txn.SsoTransactionUnavailableError):
        await txn.create_transaction(store, ttl_seconds=60, **KW)
    with pytest.raises(txn.SsoTransactionUnavailableError):
        await txn.consume_transaction(store, "some-handle")


@pytest.mark.asyncio
async def test_transaction_repr_does_not_leak_secrets(store) -> None:
    handle = await txn.create_transaction(store, ttl_seconds=60, **KW)
    record = await txn.consume_transaction(store, handle)
    assert "ver" not in repr(record).replace("org_slug", "") and "nonce" not in repr(record)
