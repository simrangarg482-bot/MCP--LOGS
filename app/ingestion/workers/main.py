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

from app.ingestion.workers.tasks import MAX_JOB_TRIES, run_ingestion_job_task, scheduled_reconciliation
from app.shared.config.logging import configure_logging
from app.shared.config.settings import get_settings
from app.shared.config.tracing import configure_worker_tracing
from app.shared.redis_settings import build_redis_settings

configure_logging()
_settings = get_settings()


async def _on_startup(ctx: dict) -> None:
    configure_worker_tracing("ekip-ingestion-worker")


class WorkerSettings:
    """arq's required entrypoint class -- discovered by name via the `arq`
    CLI command shown in this module's docstring.
    """

    functions = [run_ingestion_job_task]
    on_startup = _on_startup
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
    max_tries = MAX_JOB_TRIES
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
    # -- an even larger repo (or a slower machine) can still exceed this.
    # Page-level durable checkpoints in `app.ingestion.service` now make
    # that ceiling recoverable: the next attempt resumes remote pagination
    # rather than restarting from page one.
    job_timeout = _settings.ingestion_job_timeout_seconds
    # ARQ defaults to ten concurrent jobs. Each ingestion job performs
    # sentence-transformer inference and can hold a sizeable document batch
    # in memory; ten concurrent full syncs on a small worker cause CPU/memory
    # contention that presents to operators as random connector timeouts.
    # Keep this explicit and configurable per deployment size.
    max_jobs = _settings.ingestion_worker_max_jobs
