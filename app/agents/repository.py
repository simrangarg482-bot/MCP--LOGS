"""Persistence for agents/ -- `agent_executions` only (DATABASE_DESIGN.md's
"agents/ -- owned tables"). Pure data access, same discipline as every other
repository.py in this codebase: one statement per function, ORM rows in/out,
no business rules, no ORM->Pydantic mapping.

Writes directly to `database/` -- an ingestion/agents-parallel exception to
PROJECT_PLAN.md section 9.7's dependency list (`retrieval`, `core`, `shared`
only, `database` unlisted). The same gap already resolved three times this
project (ingestion reading `connector_configs` directly, ingestion writing
its own `ingestion_jobs`/`documents` tables, retrieval writing its own
`<collection>_chunks` tables) for an identical reason: DATABASE_DESIGN.md's
"the table's owning module writes it" convention requires *some* module to
hold a direct `database/` dependency for its own tables, and `agent_executions`
is agents-owned, not core-owned or retrieval-owned. Flagged here rather than
re-litigated, per the precedent already established for the earlier three
cases.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.agent_models import AgentExecution


async def insert_agent_execution(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_name: str,
    trigger_source: str,
    input_summary: dict | None = None,
) -> AgentExecution:
    """Create one `agent_executions` row (`status="running"`) and return it
    with server-side defaults (`id`, `started_at`) populated.
    """
    row = AgentExecution(
        organization_id=organization_id,
        agent_name=agent_name,
        trigger_source=trigger_source,
        input_summary=input_summary,
        status="running",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update_agent_execution(
    session: AsyncSession, execution_id: uuid.UUID, **fields: Any
) -> AgentExecution | None:
    """Apply `fields` to an `agent_executions` row, returning the updated
    row or None if it doesn't exist. Generic, dict-driven updater -- same
    rationale as `ingestion.repository.update_ingestion_job`: a run has
    enough independently-updatable fields (`status`, `confidence_score`,
    `error_detail`, `completed_at`) that a narrow function per field would
    multiply faster than it's worth.
    """
    row = await session.get(AgentExecution, execution_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.refresh(row)
    return row
