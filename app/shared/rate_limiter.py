"""A simple, dependency-free async token bucket (Phase 6.5 relocation).

Owned by: shared/ (cross-cutting, no business meaning of its own). Moved
here from `app.ingestion.rate_limiter` (Phase 3, "Rate limiting per
connector, per tenant") when Phase 6.5 needed the identical algorithm for
API-level request throttling (`app.api.rate_limit`) -- `app.api`/`app.core`
are both import-linter-forbidden from depending on `app.ingestion` at all,
and this class itself has no ingestion-specific logic in it (only its
*callers* -- `app.ingestion.service`'s two per-connector/per-organization
buckets -- are ingestion-specific), so relocating the generic utility to
`shared/` is the correct fix, not a workaround.

Two acquisition modes, for two different use cases:
  - `acquire()` -- **blocks** until a token is available. Correct for
    background work (an ingestion job) where waiting is free: nobody is
    holding an HTTP connection open for it, so throttling by delaying is
    strictly better than failing.
  - `try_acquire()` -- **never blocks**; returns immediately with whether a
    token was available. Correct for an HTTP request path (Phase 6.5's
    per-user/per-org/per-IP API rate limiting): a request handler should
    return `429 Too Many Requests` promptly, never silently hang a client
    for an unbounded/unpredictable duration waiting for a token to accrue.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """Keyed by an arbitrary string so one instance can back many
    independent budgets (one per connector_config, one per organization,
    one per API-endpoint-dimension) without a separate object per key
    having to be constructed and threaded through by callers.

    `capacity` (the burst allowance) defaults to `max(rate, 1.0)` --
    "one second's worth of budget," a floor of 1.0 keeping any `rate < 1.0`
    (e.g. a connector's `requests_per_second = 0.5`) from being a permanent
    deny rather than a throttle. This default is exactly right for
    `app.ingestion`'s per-*second* connector/org budgets, but is the wrong
    shape for a human-facing per-*minute* budget: a caller passing
    `rate = 20/60 ≈ 0.33` (from "20 requests per minute") would otherwise
    get a burst of only 1 token -- refilling one every three seconds, a far
    stricter and less natural throttle than "20 per minute" actually means.
    Callers computing a rate from a per-minute quota should pass that
    original quota as an explicit `capacity` (e.g. `capacity=20,
    rate=20/60`) so the burst allowance matches what "N per minute" actually
    promises, while the refill rate still enforces the real steady-state
    ceiling.
    """

    def __init__(self) -> None:
        # key -> (available_tokens, last_refill_monotonic_time)
        self._buckets: dict[str, tuple[float, float]] = {}
        # Guards read-modify-write of `_buckets` for a given key across
        # concurrent acquisition calls within this process (e.g. two jobs
        # for the same organization running concurrently, or two concurrent
        # requests from the same user) -- without this, two coroutines
        # could both read a stale token count and both proceed immediately,
        # defeating the limiter.
        self._lock = asyncio.Lock()

    def _refill(self, key: str, rate: float, capacity: float, initial_tokens: float) -> float:
        """Advance `key`'s bucket to "now" and return its current token
        count, without consuming one -- shared by both acquisition modes so
        the refill math exists in exactly one place. Caller must hold
        `self._lock`.

        `initial_tokens` is what a *never-before-seen* key starts with --
        deliberately a separate parameter from `capacity`, not always equal
        to it. See `acquire`/`try_acquire`'s own comments for why an
        explicit-`capacity` caller (Phase 6.5's API rate limiting) wants a
        cold key to start completely full (`initial_tokens == capacity`,
        an immediate full burst), while the default-capacity caller
        (`app.ingestion`'s per-second connector/org budgets) must keep
        starting a cold key at only `rate` tokens -- an existing, real
        regression test (`test_sub_one_rate_eventually_grants_a_token`)
        already depends on a sub-1/s connector's very first acquisition
        waiting, not succeeding instantly.
        """
        now = time.monotonic()
        tokens, last_refill = self._buckets.get(key, (initial_tokens, now))
        elapsed = now - last_refill
        tokens = min(capacity, tokens + elapsed * rate)
        self._buckets[key] = (tokens, now)
        return tokens

    async def acquire(self, key: str, rate: float, *, capacity: float | None = None) -> None:
        """Block until one token is available for `key` at `rate` tokens/
        second, then consume it.

        `rate <= 0` is treated as "no limit" (returns immediately) rather
        than raising or dividing by zero -- a connector or org budget of
        zero would otherwise deadlock every job for that key forever, which
        is a worse failure mode than simply not throttling an
        obviously-misconfigured rate. `capacity` defaults per the class
        docstring above; passing it explicitly also makes a *cold* key start
        completely full (an immediate burst up to `capacity`), whereas the
        default-capacity case starts a cold key at only `rate` tokens (see
        `_refill`'s own docstring for why these differ).
        """
        if rate <= 0:
            return

        effective_capacity = max(rate, 1.0) if capacity is None else capacity
        initial_tokens = rate if capacity is None else effective_capacity
        while True:
            async with self._lock:
                tokens = self._refill(key, rate, effective_capacity, initial_tokens)

                if tokens >= 1.0:
                    stored_tokens, last_refill = self._buckets[key]
                    self._buckets[key] = (stored_tokens - 1.0, last_refill)
                    return

                wait_seconds = (1.0 - tokens) / rate

            await asyncio.sleep(wait_seconds)

    async def try_acquire(self, key: str, rate: float, *, capacity: float | None = None) -> bool:
        """Non-blocking: consume one token for `key` at `rate` tokens/
        second if one is immediately available, otherwise return `False`
        without waiting at all.

        Same `rate <= 0` == "no limit" convention as `acquire()`, for the
        same reason (a misconfigured zero/negative rate must never turn
        into "deny everything forever"). Same `capacity`/cold-start
        semantics too -- see `acquire`'s own comment.
        """
        if rate <= 0:
            return True

        effective_capacity = max(rate, 1.0) if capacity is None else capacity
        initial_tokens = rate if capacity is None else effective_capacity
        async with self._lock:
            tokens = self._refill(key, rate, effective_capacity, initial_tokens)
            if tokens < 1.0:
                return False
            stored_tokens, last_refill = self._buckets[key]
            self._buckets[key] = (stored_tokens - 1.0, last_refill)
            return True
