"""One shared `arq.connections.RedisSettings` builder for every process that
talks to Redis (both `arq` workers, plus the API's own enqueue-only pool in
`app.api.main`) -- all three previously built their own via a bare
`RedisSettings.from_dsn(str(get_settings().redis_url))`, which leaves every
`redis-py` connection at arq's defaults: zero command-level retries
(`Retry(NoBackoff(), 0)`) and a 1-second connect timeout.

That default is fine against a local/same-network Redis, but this project's
non-Docker local dev (and, so far, its only deployment target) points
`REDIS_URL` at a managed Redis reached over the public internet (Redis
Cloud/Railway, see `.env.docker.example`'s "non-Docker local dev against the
real remote ... Redis Cloud" note) -- a link that drops or stalls
occasionally for reasons entirely outside this app's control. With zero
retries, ANY transient blip (a `ConnectionError`, a read timing out as
`OSError: [WinError 121] The semaphore timeout period has expired` on
Windows) raises all the way up through `redis-py`'s own retry wrapper
(`Retry.call_with_retry`) on the very first failure.

For the two `arq` worker processes, that exception surfaces inside
`Worker._poll_iteration`/`start_jobs`, which nothing catches -- it escapes
`Worker.run()` entirely and kills the whole worker process (observed in
practice: one Redis blip during a long-running ingestion sync took the
entire `arq app.ingestion.workers.main.WorkerSettings` process down, not
just the one job). `app.api.main`'s own `create_pool` call already learned
this lesson once for a *different* Redis blip (see its own docstring on an
observed Redis Cloud outage taking down the whole API) and worked around it
there by not letting a failed pool creation block startup -- but that guards
only the initial connection, not a drop occurring on an already-open pool
mid-request, which is exactly the gap this shared builder closes for all
three processes at once.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.shared.config.settings import get_settings

# arq's own default (`RedisSettings.conn_timeout`, `socket_connect_timeout`
# under the hood) is 1 second -- fine for a Redis on localhost/the same
# Docker network, too tight for a TLS handshake to a geographically distant
# managed Redis under any real load or packet loss.
_CONN_TIMEOUT_SECONDS = 10
# Bounded, not infinite: a genuinely down Redis (not just a blip) must still
# surface as a real error eventually rather than retrying forever and
# masking an actual outage. Exponential backoff (1s, 2s, 4s, 8s, capped) over
# 5 attempts covers the kind of few-second network hiccup observed in
# practice without meaningfully delaying a real, sustained outage's failure.
_RETRY_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 1
_BACKOFF_CAP_SECONDS = 8


def build_redis_settings() -> RedisSettings:
    """Build this process's `RedisSettings` from `Settings.redis_url`, with
    command-level retry (and a more forgiving connect timeout) applied on
    top -- see this module's own docstring for why every caller needs this
    rather than a bare `RedisSettings.from_dsn(...)`.
    """
    settings = RedisSettings.from_dsn(str(get_settings().redis_url))
    settings.conn_timeout = _CONN_TIMEOUT_SECONDS
    # `retry_on_timeout` additionally teaches `redis-py` to treat
    # `socket.timeout`/`asyncio.TimeoutError` as retryable, on top of
    # `Retry`'s own default `(ConnectionError, TimeoutError)` support
    # (`redis.asyncio.retry.Retry.__init__`'s default `supported_errors`) --
    # both matter here: the observed crash raised `redis.exceptions.
    # ConnectionError` first, then `redis.exceptions.TimeoutError` on the
    # reconnect attempt that followed it.
    settings.retry_on_timeout = True
    settings.retry_on_error = [RedisConnectionError, RedisTimeoutError]
    settings.retry = Retry(
        ExponentialBackoff(cap=_BACKOFF_CAP_SECONDS, base=_BACKOFF_BASE_SECONDS),
        retries=_RETRY_ATTEMPTS,
    )
    return settings
