"""Persistence for agents/knowledge_gap.

Owned by: agents/knowledge_gap. Reads `agent_executions` (an existing table
already owned by agents/, per `agent_models.AgentExecution`'s own docstring)
and owns writes to the new `knowledge_gap_reports` table
(`agent_models.KnowledgeGapReport`) -- same "one statement per function, ORM
rows in/out, no business rules" discipline as every other repository.py in
this codebase (see `core/incidents/repository.py`'s module docstring).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.agent_models import AgentExecution, KnowledgeGapReport


async def list_low_confidence_executions(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    agent_name: str,
    max_confidence: float,
    since: datetime,
) -> Sequence[AgentExecution]:
    """Return every succeeded `agent_name` execution for `organization_id`
    since `since` whose `confidence_score` is below `max_confidence` --
    the Knowledge Gap Agent's raw input (AGENT_WORKFLOWS.md: "recent
    `agent_executions` rows").

    `status == "succeeded"` only: a failed execution's low/absent confidence
    reflects a bug or transient error, not a genuine knowledge gap in the
    org's documentation -- clustering those in would conflate "the system
    broke" with "the system didn't know," exactly the distinction
    AGENT_WORKFLOWS.md section 4 already draws for user-facing responses.
    """
    stmt = (
        select(AgentExecution)
        .where(
            AgentExecution.organization_id == organization_id,
            AgentExecution.agent_name == agent_name,
            AgentExecution.status == "succeeded",
            AgentExecution.confidence_score.is_not(None),
            AgentExecution.confidence_score < max_confidence,
            AgentExecution.started_at >= since,
        )
        .order_by(AgentExecution.started_at.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_open_gap_reports(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[KnowledgeGapReport]:
    """Return every still-open gap report for `organization_id`, newest
    first -- both `GET /knowledge/gaps`'s read surface and the re-run
    merge-check's candidate set (`pipeline.py`).
    """
    stmt = (
        select(KnowledgeGapReport)
        .where(
            KnowledgeGapReport.organization_id == organization_id,
            KnowledgeGapReport.status == "open",
        )
        .order_by(KnowledgeGapReport.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def insert_gap_report(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    suggested_topic: str,
    topic_embedding: list[float],
    supporting_execution_ids: list[str],
    suggested_action: str,
    related_document_id: uuid.UUID | None,
) -> KnowledgeGapReport:
    """Create one new gap report (`status="open"`) and return it with
    server defaults populated.
    """
    row = KnowledgeGapReport(
        organization_id=organization_id,
        suggested_topic=suggested_topic,
        topic_embedding=topic_embedding,
        supporting_execution_ids=supporting_execution_ids,
        suggested_action=suggested_action,
        related_document_id=related_document_id,
        status="open",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update_gap_report_supporting_ids(
    session: AsyncSession, gap_report_id: uuid.UUID, *, supporting_execution_ids: list[str]
) -> KnowledgeGapReport | None:
    """Merge newly-clustered execution ids into an existing open report
    (`pipeline.py`'s re-run idempotency check), returning the updated row or
    None if it doesn't exist.
    """
    row = await session.get(KnowledgeGapReport, gap_report_id)
    if row is None:
        return None
    row.supporting_execution_ids = supporting_execution_ids
    await session.flush()
    await session.refresh(row)
    return row
