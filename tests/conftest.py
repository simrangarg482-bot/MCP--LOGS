"""Session-wide test fixtures.

Owned by: tests/ (applies across every test module, not any one owning
package's tests).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Clears the shared, process-wide `TokenBucketRateLimiter` singletons
    -- `app.api.rate_limit` (Phase 6.5) and `app.mcp.rate_limit` -- before
    and after every test.

    Each is deliberately a module-level singleton in application code (so
    real requests within one process share one budget per key) -- but the
    same singleton persisting across the whole pytest session means two
    unrelated tests hitting the same rate-limited surface with the same test
    identity (a common pattern: many fixtures construct
    `Identity.for_agent("test_actor", ...)`, a *constant* `subject` across
    every test using it) would otherwise accumulate against the same bucket
    and spuriously 429 a later, unrelated test. Real production behavior
    (the limiter persisting across real requests) is entirely unaffected --
    this only resets the test process's own accumulated state between tests.

    `app.api.rate_limit._limiter` is now `RedisTokenBucketRateLimiter`
    (Phase 6.5's distributed-rate-limiting fix) -- it has no `_buckets` dict
    of its own to clear (its state lives in Redis, not this process), and
    `tests/api/test_rate_limit.py` already swaps in its own throwaway
    in-process limiter per test via `monkeypatch` for exactly this
    isolation concern, so there's nothing for this fixture to do for it.
    `app.mcp.rate_limit._limiter` is unchanged (still in-process, out of
    this fix's scope) and still needs clearing.
    """
    from app.api import rate_limit as api_rate_limit
    from app.mcp import rate_limit as mcp_rate_limit

    limiters = (api_rate_limit._limiter, mcp_rate_limit._limiter)
    for limiter in limiters:
        buckets = getattr(limiter, "_buckets", None)
        if buckets is not None:
            buckets.clear()
    yield
    for limiter in limiters:
        buckets = getattr(limiter, "_buckets", None)
        if buckets is not None:
            buckets.clear()
