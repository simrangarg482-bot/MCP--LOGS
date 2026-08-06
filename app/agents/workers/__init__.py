"""arq worker for agents/'s scheduled (not per-question) agents -- currently
just the Knowledge Gap Agent (Milestone 9).

Owned by: agents/workers. A separate worker/process from
`app.ingestion.workers` (its own `WorkerSettings`, its own `arq` CLI
invocation) rather than one more cron job added to ingestion's existing
worker, because `pyproject.toml`'s "ingestion does not depend on agents or
mcp" contract forbids `app.ingestion.workers` from importing `app.agents` at
all -- there is no way to add an agents-calling cron job to that worker
without violating it. Unlike `app.mcp`, `app.agents` has no equivalent
restriction against importing `app.database` directly, so (unlike
`scripts/run_mcp_server.py`'s dependency-inversion trick) this worker opens
its own sessions via `app.database.session.session_scope` directly, inside
the package, exactly the way `app.ingestion.workers.tasks` already does for
its own worker.
"""

from __future__ import annotations
