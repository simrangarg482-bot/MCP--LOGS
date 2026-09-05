"""API-level request-rate limiting (Phase 6.5).

Owned by: app/api. Distinct from `app.ingestion`'s own per-connector/per-
organization outbound-request throttling (a different concern: bounding how
fast *this application* calls an external API) -- this module bounds how
fast a *caller of this application's own API* may hit specific endpoints.

Built as FastAPI dependencies, not a blanket middleware, deliberately:
identity (`Identity.user_id`/`organization_id`) is only known once
`get_current_identity` has already run, and different endpoints need
different dimensions (an unauthenticated login attempt has no user yet, so
it's throttled by IP; an authenticated `/ask` call is throttled by user).
A single generic middleware would need to duplicate route-matching and
identity-resolution logic FastAPI's own dependency injection already does
for free -- `Depends(rate_limit_by_...(...))` composes with existing route
dependencies instead of fighting them.

Backed by Redis (`app.shared.distributed_rate_limiter.
RedisTokenBucketRateLimiter`, the same class -- and the same atomic
Lua-script token bucket -- `app.ingestion.workers.tasks` already uses for
per-connector/per-organization throttling), not the in-process
`app.shared.rate_limiter.TokenBucketRateLimiter` this module used
previously. That in-process limiter kept a separate budget per API
process: with N replicas, a caller could get up to N times the configured
limit, since each replica's own bucket had no idea another replica had
already granted requests against the same key. Redis is the one state
every replica already shares, so it is what makes one budget actually mean
one budget regardless of how many replicas are serving traffic. See
`RedisTokenBucketRateLimiter.try_acquire`'s own docstring for this module's
Redis-unavailable behavior (fails open, does not turn a Redis blip into an
API-wide outage).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request

from redis.asyncio import Redis

from app.api.deps import CurrentIdentity
from app.core.exceptions import RateLimitedError
from app.shared.config.settings import get_settings
from app.shared.distributed_rate_limiter import RedisTokenBucketRateLimiter

# One shared limiter for every rate-limited endpoint across every API
# replica -- `scope` (passed by each dependency factory below) namespaces
# keys so different endpoints' budgets never collide even for the same
# caller. `Redis.from_url` is lazy (no connection is opened until the first
# command), so constructing this at import time, the same way the
# in-process limiter it replaces was constructed, does not block or risk
# API startup -- consistent with `RedisTokenBucketRateLimiter.try_acquire`'s
# own fail-open handling of a connection that never succeeds.
_limiter = RedisTokenBucketRateLimiter(Redis.from_url(str(get_settings().redis_url)))


def _client_ip(request: Request) -> str:
    """Best-effort caller IP. `request.client` is `None` in some ASGI test
    contexts (never in a real deployment behind an actual TCP connection),
    falling back to a fixed key rather than raising -- a rate limiter that
    crashes when it can't identify the caller is worse than one that
    (rarely, only in that edge case) shares one bucket across such callers.
    """
    if request.client is not None:
        return request.client.host
    return "unknown"


def rate_limit_by_ip(
    *, scope: str, requests_per_minute: float
) -> Callable[[Request], Coroutine[Any, Any, None]]:
    """Returns a dependency throttling by caller IP -- for endpoints with no
    `Identity` yet (login, signup): a per-user/per-org key isn't available
    before authentication succeeds, and IP is the only caller-identifying
    dimension that exists pre-auth. Also serves as this codebase's first
    concrete defense against credential-stuffing/brute-force login attempts,
    which nothing previously bounded at all.

    `capacity=requests_per_minute` (not the token bucket's own `rate`-based
    default): the burst allowance should match the full per-minute quota --
    see `TokenBucketRateLimiter`'s own docstring for why leaving this at the
    default would make e.g. "10 per minute" behave as "1 burst request, then
    one every 6 seconds" instead.
    """
    rate = requests_per_minute / 60.0

    async def _dependency(request: Request) -> None:
        key = f"{scope}:ip:{_client_ip(request)}"
        if not await _limiter.try_acquire(key, rate, capacity=requests_per_minute):
            raise RateLimitedError(
                "Too many requests. Please wait before trying again.",
                error_code="rate_limited.ip",
                detail={"scope": scope},
            )

    return _dependency


def rate_limit_by_user(
    *, scope: str, requests_per_minute: float
) -> Callable[[CurrentIdentity], Coroutine[Any, Any, None]]:
    """Returns a dependency throttling by authenticated user -- for
    endpoints like `/ask`/search/investigation where each real human should
    get their own budget, independent of how many other users their
    organization has. See `rate_limit_by_ip`'s own comment for why
    `capacity=requests_per_minute` is passed explicitly.
    """
    rate = requests_per_minute / 60.0

    async def _dependency(actor: CurrentIdentity) -> None:
        key = f"{scope}:user:{actor.user_id or actor.subject}"
        if not await _limiter.try_acquire(key, rate, capacity=requests_per_minute):
            raise RateLimitedError(
                "Too many requests. Please wait before trying again.",
                error_code="rate_limited.user",
                detail={"scope": scope},
            )

    return _dependency


def rate_limit_by_org(
    *, scope: str, requests_per_minute: float
) -> Callable[[CurrentIdentity], Coroutine[Any, Any, None]]:
    """Returns a dependency throttling by organization -- for endpoints
    where the *aggregate* load one tenant places on a shared resource
    matters more than any one user's individual request rate (e.g.
    connector sync: five different users in one organization each
    triggering a sync is still one organization's ingestion load). See
    `rate_limit_by_ip`'s own comment for why `capacity=requests_per_minute`
    is passed explicitly.
    """
    rate = requests_per_minute / 60.0

    async def _dependency(actor: CurrentIdentity) -> None:
        key = f"{scope}:org:{actor.organization_id}"
        if not await _limiter.try_acquire(key, rate, capacity=requests_per_minute):
            raise RateLimitedError(
                "This organization has made too many requests. Please wait before trying again.",
                error_code="rate_limited.organization",
                detail={"scope": scope},
            )

    return _dependency
