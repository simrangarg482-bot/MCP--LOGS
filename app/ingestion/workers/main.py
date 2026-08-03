"""arq worker process entrypoint.

Owned by: ingestion/workers/. Run as its own OS process, separate from the
API server (PROJECT_PLAN.md section 4.5, ENGINEERING_DECISIONS.md #002):

    arq app.ingestion.workers.main.WorkerSettings

`redis_settings` is built from the same `Settings.redis_url` every other
part of the app reads (`app.shared.config.settings`) -- one source of truth
for the Redis connection string, not a second one hand-maintained here.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.cron import cron

from app.ingestion.workers.tasks import run_ingestion_job_task, scheduled_reconciliation
from app.shared.config.logging import configure_logging
from app.shared.config.settings import get_settings

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
    redis_settings = RedisSettings.from_dsn(str(get_settings().redis_url))
    # Bounded max-attempt count (PROJECT_PLAN.md section 4.5). The
    # exponential backoff itself is implemented in
    # `run_ingestion_job_task` via `arq.jobs.Retry(defer=...)`, not here --
    # arq's own default retry has no backoff built in, so relying on
    # `max_tries` alone would satisfy only the "bounded" half of section
    # 4.5's requirement, not the "exponential backoff" half.
    max_tries = 3
