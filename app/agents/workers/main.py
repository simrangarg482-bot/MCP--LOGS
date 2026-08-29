"""arq worker process entrypoint for agents/'s scheduled agents. Run as its
own process, separate from both the API server and the ingestion worker:

    arq app.agents.workers.main.WorkerSettings

`redis_settings` comes from `app.shared.redis_settings.build_redis_settings`,
the same single-source-of-truth convention `app.ingestion.workers.main`'s
identical line already follows -- see that helper's own docstring for why a
bare `RedisSettings.from_dsn(...)` isn't enough.
"""

from __future__ import annotations

from arq.cron import cron

from app.agents.workers.tasks import (
    run_knowledge_gap_detection_task,
    run_pattern_detection_task,
    scheduled_knowledge_gap_scan,
    scheduled_pattern_detection_scan,
)
from app.shared.config.logging import configure_logging
from app.shared.redis_settings import build_redis_settings

configure_logging()


class WorkerSettings:
    """arq's required entrypoint class -- discovered by name via the `arq`
    CLI command shown in this module's docstring.
    """

    functions = [run_knowledge_gap_detection_task, run_pattern_detection_task]
    # Daily at 02:00 -- deliberately much less frequent than ingestion's
    # hourly reconciliation: a documentation gap is, by definition, a
    # *repeated* pattern accumulated over `knowledge_gap_lookback_days`
    # (default 14) of history, not something that meaningfully changes
    # hour to hour the way "has a new commit landed" does. Running hourly
    # would mostly re-scan the same low-confidence executions and re-merge
    # into the same open reports for no benefit.
    #
    # Priority 6's pattern-detection scan runs every 6 hours (00/06/12/18) --
    # a deliberate middle point between the two existing cadences here.
    # Unlike the knowledge-gap scan, its detectors are cheap (a couple of
    # bounded, indexed SQL queries, no LLM calls at all -- see
    # `core.proactive.contract`'s module docstring), so there is no cost
    # reason to run it only once a day; unlike ingestion's hourly sync,
    # nothing about "3 incidents in a 14-day window" changes meaningfully
    # hour to hour either, so there is no freshness reason to match
    # ingestion's cadence. Reusing this same worker process/queue (rather
    # than standing up a third one) is itself the smallest-truthful-
    # integration choice this priority's spec asks for -- see
    # `docs/PROACTIVE_INTELLIGENCE.md`.
    cron_jobs = [
        cron(scheduled_knowledge_gap_scan, hour=2, minute=0),
        cron(scheduled_pattern_detection_scan, hour={0, 6, 12, 18}, minute=0),
    ]
    redis_settings = build_redis_settings()
    # See `app.ingestion.workers.main.WorkerSettings.queue_name`'s comment --
    # without an explicit, distinct queue name here too, this worker shares
    # arq's default queue with the ingestion worker on the same Redis
    # instance and can steal its jobs, failing them permanently.
    queue_name = "arq:queue:agents"
    # Same bounded max-attempt count as `app.ingestion.workers.main` -- see
    # that class's own comment on why the backoff itself lives in the task
    # function (`Retry(defer=...)`), not here.
    max_tries = 3
    # Phase 6.1/6.3: explicit, not arq's own 300s default -- left unset
    # previously despite `app.ingestion.workers.main` explicitly widening
    # its own job_timeout to 1800s for an analogous "could scale with
    # organization volume" concern, an asymmetry that was undocumented
    # rather than a deliberate choice. `detect_knowledge_gaps` clusters
    # every low-confidence `agent_executions` row over
    # `knowledge_gap_lookback_days` (default 14) and makes a handful of LLM
    # calls per resulting cluster (`_synthesize_topic`/
    # `_resolve_suggested_action`) -- lighter per-organization work than
    # ingestion's "fetch and process every document from an external
    # source," but not bounded at a fixed cost either for an organization
    # with a very large low-confidence-query volume. 600s (10 min) is a
    # deliberate middle value: generous enough for a large single-org
    # clustering pass without blindly copying ingestion's 1800s for a
    # meaningfully different workload shape.
    job_timeout = 600
