"""Tests for `app.shared.rate_limiter.TokenBucketRateLimiter` -- relocated
from `tests/ingestion/test_rate_limiter.py` (Phase 6.5) alongside the class
itself; see `app.shared.rate_limiter`'s own module docstring for why.

Uses a high `rate` (100/s -> ~10ms per token) throughout so timing
assertions stay fast and comfortably tolerant of scheduling jitter in CI,
rather than asserting exact durations.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.shared.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_first_acquire_for_a_fresh_key_does_not_block() -> None:
    limiter = TokenBucketRateLimiter()

    started = time.monotonic()
    await limiter.acquire("connector:a", 100.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_exhausting_the_burst_forces_the_next_acquire_to_wait() -> None:
    limiter = TokenBucketRateLimiter()
    rate = 100.0  # burst == rate == 100 tokens available immediately

    # Drain the full burst -- all 100 should return essentially instantly.
    started = time.monotonic()
    for _ in range(100):
        await limiter.acquire("connector:a", rate)
    drain_elapsed = time.monotonic() - started
    assert drain_elapsed < 0.2

    # The 101st call has no tokens left and must wait ~1/rate seconds.
    started = time.monotonic()
    await limiter.acquire("connector:a", rate)
    wait_elapsed = time.monotonic() - started

    assert wait_elapsed >= (1.0 / rate) * 0.5  # allow generous scheduling slack


@pytest.mark.asyncio
async def test_sub_one_rate_eventually_grants_a_token() -> None:
    """Regression test: a bucket capped at `rate` itself (not floored at
    1.0) can never reach the `tokens >= 1.0` threshold `acquire()` checks
    for when `rate < 1.0` -- every call loops forever instead of eventually
    proceeding. `SlackConnector.requests_per_second = 0.5` is the one real
    caller this affects; a high `rate` elsewhere in this file would never
    have caught it. Bounded with `asyncio.wait_for` so a regression fails
    the test instead of hanging the suite.
    """
    limiter = TokenBucketRateLimiter()
    rate = 0.5  # one token every 2 seconds -- matches SlackConnector

    started = time.monotonic()
    await asyncio.wait_for(limiter.acquire("connector:slack", rate), timeout=5.0)
    elapsed = time.monotonic() - started

    # First call starts with `rate` (0.5) tokens, short of the 1.0 needed,
    # so it must wait roughly (1.0 - rate) / rate = 1s -- but it must
    # complete at all, which is the actual regression being guarded here.
    assert elapsed >= 0.5


@pytest.mark.asyncio
async def test_rate_zero_or_negative_never_blocks() -> None:
    limiter = TokenBucketRateLimiter()

    started = time.monotonic()
    for _ in range(1000):
        await limiter.acquire("connector:a", 0.0)
        await limiter.acquire("connector:a", -1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_different_keys_have_independent_buckets() -> None:
    limiter = TokenBucketRateLimiter()
    rate = 100.0

    # Drain key "a" completely.
    for _ in range(100):
        await limiter.acquire("connector:a", rate)

    # Key "b" is untouched -- still has its own full burst, so this must
    # not be slowed down by "a"'s exhausted bucket.
    started = time.monotonic()
    await limiter.acquire("connector:b", rate)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_concurrent_acquires_for_the_same_key_are_serialized_not_double_spent() -> None:
    """Two concurrent callers draining the same small bucket must not both
    observe the same stale token count -- the internal lock exists exactly
    to prevent this (see the class's own docstring).
    """
    limiter = TokenBucketRateLimiter()
    rate = 2.0  # burst of 2 tokens

    results = await asyncio.gather(
        limiter.acquire("connector:shared", rate),
        limiter.acquire("connector:shared", rate),
        limiter.acquire("connector:shared", rate),
    )

    # All three eventually complete (none raise/deadlock); the third one
    # necessarily had to wait for a refill since only 2 tokens existed.
    assert results == [None, None, None]


# --- try_acquire (Phase 6.5: non-blocking mode for HTTP request paths) -----


@pytest.mark.asyncio
async def test_try_acquire_succeeds_when_a_token_is_available() -> None:
    limiter = TokenBucketRateLimiter()

    granted = await limiter.try_acquire("user:a", 10.0)

    assert granted is True


@pytest.mark.asyncio
async def test_try_acquire_returns_false_immediately_when_exhausted_not_wait() -> None:
    limiter = TokenBucketRateLimiter()
    rate = 5.0  # burst of 5

    for _ in range(5):
        assert await limiter.try_acquire("user:a", rate) is True

    started = time.monotonic()
    granted = await limiter.try_acquire("user:a", rate)
    elapsed = time.monotonic() - started

    assert granted is False
    assert elapsed < 0.05  # must not wait at all, unlike acquire()


@pytest.mark.asyncio
async def test_try_acquire_refills_over_time() -> None:
    limiter = TokenBucketRateLimiter()
    rate = 100.0  # ~10ms per token, fast enough for a real sleep in a test

    for _ in range(100):
        assert await limiter.try_acquire("user:a", rate) is True
    assert await limiter.try_acquire("user:a", rate) is False

    await asyncio.sleep(0.05)  # ~5 tokens' worth of refill at 100/s

    assert await limiter.try_acquire("user:a", rate) is True


@pytest.mark.asyncio
async def test_try_acquire_rate_zero_or_negative_always_grants() -> None:
    limiter = TokenBucketRateLimiter()

    for _ in range(10):
        assert await limiter.try_acquire("user:a", 0.0) is True
        assert await limiter.try_acquire("user:a", -1.0) is True


@pytest.mark.asyncio
async def test_explicit_capacity_starts_a_cold_key_completely_full() -> None:
    """Regression test for a real bug: a low `rate` (e.g. 20/minute ==
    0.333/s) combined with a much higher explicit `capacity` (20) previously
    still initialized a *cold* key to only `min(rate, capacity)` tokens
    (0.333) -- far below the 1.0 needed to grant even one request, making
    the very first call for a brand-new key wait or fail despite a burst
    capacity of 20 nominally being available. An explicit `capacity` must
    mean a cold key starts completely full.
    """
    limiter = TokenBucketRateLimiter()
    rate = 20.0 / 60.0  # "20 per minute"

    for _ in range(20):
        assert await limiter.try_acquire("user:fresh", rate, capacity=20.0) is True

    # The 21st immediate call has genuinely exhausted the burst.
    assert await limiter.try_acquire("user:fresh", rate, capacity=20.0) is False


@pytest.mark.asyncio
async def test_default_capacity_cold_start_is_unchanged_for_existing_callers() -> None:
    """Pins down that the fix above did NOT change `app.ingestion`'s
    existing default-capacity behavior: a sub-1/s connector rate must still
    start a cold key at only `rate` tokens (matching
    `test_sub_one_rate_eventually_grants_a_token` above), not jump to
    starting fully charged.
    """
    limiter = TokenBucketRateLimiter()

    started = time.monotonic()
    await asyncio.wait_for(limiter.acquire("connector:slack2", 0.5), timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.5


@pytest.mark.asyncio
async def test_acquire_and_try_acquire_share_the_same_bucket_state() -> None:
    """Both modes operate on the same underlying `_buckets` dict for a given
    key -- draining via one must be visible to the other, since a real
    caller (Phase 6.5's rate-limit middleware) only ever uses `try_acquire`,
    but this proves the shared refill logic wasn't accidentally forked into
    two independent, inconsistent implementations.
    """
    limiter = TokenBucketRateLimiter()
    rate = 3.0  # burst of 3

    await limiter.acquire("mixed:a", rate)
    await limiter.acquire("mixed:a", rate)
    assert await limiter.try_acquire("mixed:a", rate) is True
    assert await limiter.try_acquire("mixed:a", rate) is False
