"""Rate limiting for ingestion jobs (PROJECT_PLAN.md sections 4.5/10:
"Rate limiting per connector, per tenant... the worker pool enforces this
per-`connector_config`... critically, per organization's connection, not
globally").

Every connector already *declares* a `requests_per_second` ceiling (see
`app.ingestion.connectors.base.Connector`'s own docstring), but until this
module existed, nothing actually enforced it -- `app.ingestion.workers.
tasks.scheduled_reconciliation`'s own docstring flagged this explicitly
("each enqueued job is independently rate-limited per connector_config...
not attempted here"). This module closes that gap.

Two independent buckets per job, both acquired before every `fetch_batch`
call in `ingestion.service._execute_ingestion_job`'s fetch loop:
  1. A **per-connector_config** bucket, at that connector's own declared
     `requests_per_second` -- keeps one connector's sync within the ceiling
     it declared for itself.
  2. A **per-organization** bucket, at a fixed aggregate cap
     (`settings.ingestion_org_max_requests_per_second`) -- keeps one
     organization's *combined* connectors (e.g. Jira + Confluence + GitHub
     all syncing at once) from collectively exceeding a shared budget, even
     if each individually stays under its own ceiling. This is the "per
     tenant" half of the requirement a purely per-connector limiter would
     miss entirely.

Known, disclosed limitation: this is an **in-process** token bucket (a
module-level dict, not Redis-backed) -- correct for a single worker process,
but multiple concurrent worker *processes* (arq supports running more than
one) would each enforce their own, independent view of the same budget,
effectively multiplying the real ceiling by the process count. A
Redis-backed distributed token bucket (using the same Redis instance the
job queue already depends on, ENGINEERING_DECISIONS.md #003) is the correct
production fix and is flagged here as follow-up work, not silently assumed
solved.

Also disclosed: one `fetch_batch` call can itself perform more than one real
outbound HTTP request internally (e.g. `GitHubConnector`'s per-file content
fetch, `AzureDevOpsConnector`'s WIQL-then-batch-fetch pair) -- acquiring one
token per `fetch_batch` call is therefore an approximation of "requests per
second," not an exact per-HTTP-call throttle. Building a precise per-call
throttle would mean instrumenting six connectors' internal `httpx` calls
individually, a meaningfully larger change than this pass's scope.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """A simple, dependency-free async token bucket, keyed by an arbitrary
    string so one instance can back many independent budgets (one per
    connector_config, one per organization) without a separate object per
    key having to be constructed and threaded through by callers.

    Burst capacity equals `rate` itself (one second's worth of budget) --
    simple and adequate for this pass; not independently configurable, since
    no caller has yet needed a burst allowance different from its own
    steady-state rate.
    """

    def __init__(self) -> None:
        # key -> (available_tokens, last_refill_monotonic_time)
        self._buckets: dict[str, tuple[float, float]] = {}
        # Guards read-modify-write of `_buckets` for a given key across
        # concurrent `acquire` calls within this process (e.g. two jobs for
        # the same organization running concurrently) -- without this, two
        # coroutines could both read a stale token count and both proceed
        # immediately, defeating the limiter.
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, rate: float) -> None:
        """Block until one token is available for `key` at `rate` tokens/
        second, then consume it.

        `rate <= 0` is treated as "no limit" (returns immediately) rather
        than raising or dividing by zero -- a connector or org budget of
        zero would otherwise deadlock every job for that key forever, which
        is a worse failure mode than simply not throttling an
        obviously-misconfigured rate.
        """
        if rate <= 0:
            return

        while True:
            async with self._lock:
                now = time.monotonic()
                tokens, last_refill = self._buckets.get(key, (rate, now))
                elapsed = now - last_refill
                tokens = min(rate, tokens + elapsed * rate)

                if tokens >= 1.0:
                    self._buckets[key] = (tokens - 1.0, now)
                    return

                # Not enough tokens yet -- record the refill we just
                # accounted for, compute how long until one more token
                # accrues, and release the lock while waiting so other
                # keys' `acquire` calls aren't blocked behind this sleep.
                self._buckets[key] = (tokens, now)
                wait_seconds = (1.0 - tokens) / rate

            await asyncio.sleep(wait_seconds)
