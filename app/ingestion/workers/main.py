"""arq worker process entrypoint.

Owned by: ingestion/workers/. Run as its own OS process, separate from the
API server (PROJECT_PLAN.md section 4.5, ENGINEERING_DECISIONS.md #002):

    arq app.ingestion.workers.main.WorkerSettings

`redis_settings` comes from `app.shared.redis_settings.build_redis_settings`,
shared with `app.agents.workers.main` and `app.api.main` -- one source of
truth for both the Redis connection string and the retry/timeout settings
that make a transient drop against a remote Redis survivable instead of
crashing this whole process (see that module's own docstring).
"""

from __future__ import annotations

from arq.cron import cron

from app.ingestion.workers.tasks import run_ingestion_job_task, scheduled_reconciliation
from app.shared.config.logging import configure_logging
from app.shared.redis_settings import build_redis_settings

configure_logging()


class WorkerSettings:
    """arq's required entrypoint class -- discovered by name via the `arq`
    CLI command shown in this module's docstring.
    """

    functions = [run_ingestion_job_task]
    # Hourly reconciliation (minute=0, every hour) -- PROJECT_PLAN.md
    # section 4.4's "periodic reconciliation pass ... even for
    # webhook-supported sources". Omitting `hour` means "every hour", per
    # arq's cron field semantics (an omitted field matches any value, the
    # same convention as a bare `*` in crontab syntax).
    cron_jobs = [cron(scheduled_reconciliation, minute=0)]
    redis_settings = build_redis_settings()
    # arq defaults every Worker to the same hardcoded queue name
    # ("arq:queue") regardless of `functions` -- with no override, this
    # worker and `app.agents.workers.main`'s worker share one Redis queue on
    # the same `Settings.redis_url`, so either one can pop a job it has no
    # matching function for (`JobExecutionFailed: function ... not found`,
    # a permanent, unretried failure) whenever both run at once, which is
    # the normal, documented deployment shape (both are expected to run
    # simultaneously). A distinct queue name per worker is required, not
    # cosmetic.
    queue_name = "arq:queue:ingestion"
    # Bounded max-attempt count (PROJECT_PLAN.md section 4.5). The
    # exponential backoff itself is implemented in
    # `run_ingestion_job_task` via `arq.jobs.Retry(defer=...)`, not here --
    # arq's own default retry has no backoff built in, so relying on
    # `max_tries` alone would satisfy only the "bounded" half of section
    # 4.5's requirement, not the "exponential backoff" half.
    max_tries = 3
    # arq's own default (300s) is tight for a first *full* sync: each
    # `fetch_batch` call is throttled by the per-connector token bucket in
    # `app.shared.rate_limiter` (e.g. Slack's own declared
    # `requests_per_second = 0.5`), and a channel/repo with real history
    # can need enough pages that the wait time alone exceeds 300s -- arq
    # then cancels the job mid-page rather than the connector or the app
    # failing outright.
    #
    # Raised from 1800s (30 min) to 3600s (1 hour) after a real timeout
    # observed against an actual GitHub connector: `app.ingestion.
    # connectors.github`'s full sync (`since=None`, the only kind a first
    # sync ever is) runs FOUR phases per configured repo -- `"files"` (a
    # full recursive tree walk of every file currently in the repo,
    # independent of commit count and often the largest phase by volume),
    # `"commits"`, `"pulls"`, and `"issues"` -- and every embedding in
    # every phase is a synchronous, CPU-bound `sentence-transformers`
    # call (`app.retrieval.embedding.embed_texts`, `asyncio.to_thread`),
    # one call per document, competing for the same CPU as everything
    # else running on the machine. 30 minutes was a reasoned estimate, not
    # a measurement; this is the measurement. Still not unbounded headroom
    # -- an even larger repo (or a slower machine) can still exceed this --
    # see `app.ingestion.service._execute_ingestion_job`'s own docstring
    # for why true stage-level resume (rather than a bigger fixed ceiling)
    # is the real fix for that, flagged there as a larger, separate
    # undertaking rather than assumed already solved by this number.
    job_timeout = 3600
