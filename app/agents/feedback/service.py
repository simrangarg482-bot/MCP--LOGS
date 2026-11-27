"""Public interface for `agents.feedback`: submit/read one's own rating of an
AI execution, and the curator-facing aggregate insights.

AUTHORIZATION IS STRUCTURAL
    The organization and user come only from the `Identity`; the execution id
    comes from the URL and is re-validated here. A rating is accepted only
    when ALL of these hold, and every "no" except the last is reported as the
    same `NotFoundError`, so the response never confirms that another
    organization's -- or another user's -- execution id exists:

      * the caller is a human user (`actor.user_id`);
      * the execution exists in the caller's organization (RLS + explicit
        filter);
      * the caller is the user who triggered it (you rate your own answers);
      * the execution is one that produces a rateable answer
        (`answer_question` / `triage_incident`);
      * and it succeeded -- feedback on a run that failed would rate a
        generic error message, not an answer (-> 409).

    Raw feedback is private to its author: there is no endpoint that lists
    another user's rating. Curators with `knowledge:review` get only
    aggregates and redacted, truncated question text -- never who asked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.feedback import patterns, repository
from app.agents.feedback.redaction import redact_secrets
from app.agents.feedback.schemas import (
    MAX_COMMENT_LENGTH,
    Feedback,
    FeedbackInsights,
    FeedbackSubmit,
)
from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.users.service import require_permission
from app.retrieval import embedding
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import Identity

logger = get_logger(__name__)

#: Executions whose output is an answer a person can judge.
RATEABLE_AGENTS = frozenset({"answer_question", "triage_incident"})

#: Same permission that gates the knowledge review queue and gap reports --
#: the people who curate knowledge quality are the ones who read this.
_INSIGHTS_PERMISSION = "knowledge:review"

#: Insights work on a bounded recent slice, never an unbounded scan.
_INSIGHTS_ROW_LIMIT = 500
_MAX_WINDOW_DAYS = 365


def _not_found() -> NotFoundError:
    return NotFoundError(
        "Execution not found.", error_code="feedback.execution_not_found"
    )


async def submit_feedback(
    session: AsyncSession,
    actor: Identity,
    execution_id: uuid.UUID,
    data: FeedbackSubmit,
) -> Feedback:
    """Record, or change, the caller's rating of `execution_id`.

    Idempotent: resubmitting an identical rating changes nothing and writes no
    audit event. Changing a rating updates the one row in place.
    """
    if actor.user_id is None:
        raise PermissionDeniedError(
            "Only signed-in users can rate answers.", error_code="feedback.requires_user"
        )

    execution = await repository.get_execution(
        session, organization_id=actor.organization_id, execution_id=execution_id
    )
    if (
        execution is None
        or execution.user_id != actor.user_id
        or execution.agent_name not in RATEABLE_AGENTS
    ):
        raise _not_found()
    if execution.status != "succeeded":
        raise ConflictError(
            "Feedback can only be given on an execution that completed successfully.",
            error_code="feedback.execution_not_rateable",
            detail={"status": execution.status},
        )

    # Redact BEFORE persisting (and before comparing, so an identical
    # resubmission is recognised as identical). Truncate afterwards: a
    # redaction marker can be longer than the secret it replaced, and the
    # column is constrained to MAX_COMMENT_LENGTH.
    comment: str | None = None
    redactions = 0
    if data.comment is not None:
        comment, redactions = redact_secrets(data.comment)
        comment = comment[:MAX_COMMENT_LENGTH]
    # A reason only qualifies a negative rating.
    reason_code = data.reason_code if data.rating == "not_helpful" else None

    existing = await repository.get_feedback(
        session,
        organization_id=actor.organization_id,
        execution_id=execution_id,
        user_id=actor.user_id,
    )
    if (
        existing is not None
        and existing.rating == data.rating
        and existing.reason_code == reason_code
        and existing.comment == comment
    ):
        return Feedback.model_validate(existing)

    row = await repository.upsert_feedback(
        session,
        organization_id=actor.organization_id,
        execution_id=execution_id,
        user_id=actor.user_id,
        rating=data.rating,
        reason_code=reason_code,
        comment=comment,
    )
    await record_audit_event(
        session,
        actor,
        action="answer_feedback.update" if existing is not None else "answer_feedback.submit",
        resource_type="answer_feedback",
        resource_id=row.id,
        # Never the comment text -- only that one exists.
        metadata={
            "execution_id": str(execution_id),
            "rating": data.rating,
            "reason_code": reason_code,
            "has_comment": comment is not None,
            "comment_redactions": redactions,
        },
    )
    return Feedback.model_validate(row)


async def get_my_feedback(
    session: AsyncSession, actor: Identity, execution_id: uuid.UUID
) -> Feedback | None:
    """The caller's own rating of `execution_id`, or None. Lets the UI show
    prior feedback after a reload. Only ever the caller's own row."""
    if actor.user_id is None:
        raise PermissionDeniedError(
            "Only signed-in users can rate answers.", error_code="feedback.requires_user"
        )
    row = await repository.get_feedback(
        session,
        organization_id=actor.organization_id,
        execution_id=execution_id,
        user_id=actor.user_id,
    )
    return Feedback.model_validate(row) if row is not None else None


async def get_insights(
    session: AsyncSession, actor: Identity, *, window_days: int = 30
) -> FeedbackInsights:
    """Aggregate view for knowledge curators. Read-only; see `patterns`."""
    require_permission(actor, _INSIGHTS_PERMISSION)
    window_days = max(1, min(window_days, _MAX_WINDOW_DAYS))
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = await repository.list_feedback_with_executions(
        session,
        organization_id=actor.organization_id,
        since=since,
        limit=_INSIGHTS_ROW_LIMIT,
    )
    found = await patterns.identify_patterns(
        rows,
        embed=embedding.embed_texts,
        similarity_threshold=get_settings().knowledge_gap_similarity_threshold,
    )
    return FeedbackInsights(
        window_days=window_days,
        totals=patterns.summarize_totals(rows),
        calibration=patterns.summarize_calibration(rows),
        patterns=found,
    )
