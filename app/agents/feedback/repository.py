"""Persistence for `agents.feedback` -- `answer_feedback` writes plus the
read-only joins against `agent_executions` the insights need.

Same discipline as every other repository.py here: one statement per
function, ORM rows in/out, no business rules. Every statement filters by
`organization_id` explicitly even though RLS already enforces it -- defense in
depth, the precedent `agents.repository.list_agent_executions_for_user` sets.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.agent_models import AgentExecution, AnswerFeedback


async def get_execution(
    session: AsyncSession, *, organization_id: uuid.UUID, execution_id: uuid.UUID
) -> AgentExecution | None:
    stmt = select(AgentExecution).where(
        AgentExecution.id == execution_id,
        AgentExecution.organization_id == organization_id,
    )
    return (await session.execute(stmt)).scalars().first()


async def get_feedback(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    execution_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AnswerFeedback | None:
    stmt = select(AnswerFeedback).where(
        AnswerFeedback.organization_id == organization_id,
        AnswerFeedback.execution_id == execution_id,
        AnswerFeedback.user_id == user_id,
    )
    return (await session.execute(stmt)).scalars().first()


async def upsert_feedback(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    execution_id: uuid.UUID,
    user_id: uuid.UUID,
    rating: str,
    reason_code: str | None,
    comment: str | None,
) -> AnswerFeedback:
    """Insert, or update in place on the `(execution_id, user_id)` conflict.

    A single atomic statement, so two concurrent submissions (a double click,
    two tabs) converge on one row instead of one of them failing on the unique
    constraint. `created_at` is preserved on update; `updated_at` moves.
    """
    insert_stmt = pg_insert(AnswerFeedback).values(
        organization_id=organization_id,
        execution_id=execution_id,
        user_id=user_id,
        rating=rating,
        reason_code=reason_code,
        comment=comment,
    )
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_answer_feedback_execution_user",
        set_={
            "rating": insert_stmt.excluded.rating,
            "reason_code": insert_stmt.excluded.reason_code,
            "comment": insert_stmt.excluded.comment,
            "updated_at": func.now(),
        },
    ).returning(AnswerFeedback)
    row = (await session.execute(stmt)).scalars().one()
    await session.refresh(row)
    return row



async def list_feedback_with_executions(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    since: datetime,
    limit: int,
) -> Sequence[tuple[AnswerFeedback, AgentExecution]]:
    """Most recent feedback in the window joined to its execution, newest
    first. `limit` bounds the work insights do on a busy organization."""
    stmt = (
        select(AnswerFeedback, AgentExecution)
        .join(
            AgentExecution,
            (AgentExecution.id == AnswerFeedback.execution_id)
            & (AgentExecution.organization_id == AnswerFeedback.organization_id),
        )
        .where(
            AnswerFeedback.organization_id == organization_id,
            AnswerFeedback.created_at >= since,
        )
        .order_by(AnswerFeedback.created_at.desc())
        .limit(limit)
    )
    return [(fb, ex) for fb, ex in (await session.execute(stmt)).all()]
