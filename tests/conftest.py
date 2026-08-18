"""Session-wide test fixtures.

Owned by: tests/ (applies across every test module, not any one owning
package's tests).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_api_rate_limiter():
    """Clears `app.api.rate_limit`'s shared, process-wide `TokenBucketRateLimiter`
    state before every test (Phase 6.5).

    That limiter is deliberately a module-level singleton in application
    code (so real requests within one process share one budget per key) --
    but the same singleton persisting across the whole pytest session means
    two unrelated tests hitting the same rate-limited endpoint with the same
    test identity (a common pattern: many fixtures construct
    `Identity.for_agent("test_actor", ...)`, a *constant* `subject` across
    every test using it) would otherwise accumulate against the same bucket
    and spuriously 429 a later, unrelated test. Real production behavior
    (the limiter persisting across real requests) is entirely unaffected --
    this only resets the test process's own accumulated state between tests.
    """
    from app.api import rate_limit as rate_limit_module

    rate_limit_module._limiter._buckets.clear()
    yield
    rate_limit_module._limiter._buckets.clear()
