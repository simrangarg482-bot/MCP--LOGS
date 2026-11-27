"""Outcome feedback router -- rate an AI answer, and curator-facing aggregates.

Owned by: app/api. Thin pass-through to `app.agents.feedback.service`; no
business logic beyond request/response translation.

AUTHORIZATION IS STRUCTURAL, NOT PARAMETRIC
    No endpoint accepts an `organization_id` or `user_id`. Tenancy and the
    rating's author come from the authenticated `Identity`; the only id the
    client supplies is the execution being rated, and the service re-validates
    that it exists in the caller's organization and was triggered by the
    caller. See `agents.feedback.service`'s docstring.

    There is deliberately no endpoint listing individual ratings: raw
    feedback is private to its author. Curators (`knowledge:review`) get
    aggregates and redacted question text only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.agents.feedback import service as feedback_service
from app.agents.feedback.schemas import Feedback, FeedbackInsights, FeedbackSubmit
from app.api.deps import CurrentIdentity, DbSession
from app.api.rate_limit import rate_limit_by_user

router = APIRouter(tags=["feedback"])

# Generous for a human clicking buttons, tight enough to stop a script
# hammering the audit log.
_FEEDBACK_RATE_LIMIT = rate_limit_by_user(scope="ask.feedback", requests_per_minute=60)


@router.put(
    "/ask/executions/{execution_id}/feedback",
    response_model=Feedback,
    dependencies=[Depends(_FEEDBACK_RATE_LIMIT)],
)
async def submit_feedback(
    execution_id: uuid.UUID, data: FeedbackSubmit, actor: CurrentIdentity, session: DbSession
) -> Feedback:
    """Rate the answer produced by `execution_id` (the `executionId` returned
    by `POST /ask`).

    `PUT`, not `POST`: the caller has exactly one rating per execution, so
    this creates it or replaces it, and sending the same body twice is
    harmless. A `reasonCode`/`comment` is optional and never required -- not
    even for `not_helpful`.

    An execution that is not yours, not in your organization, or does not
    exist is reported identically as 404.
    """
    return await feedback_service.submit_feedback(session, actor, execution_id, data)


@router.get("/ask/executions/{execution_id}/feedback", response_model=Feedback | None)
async def get_my_feedback(
    execution_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Feedback | None:
    """The caller's own rating of this execution, or `null` if none."""
    return await feedback_service.get_my_feedback(session, actor, execution_id)


@router.get("/feedback/insights", response_model=FeedbackInsights)
async def get_feedback_insights(
    actor: CurrentIdentity,
    session: DbSession,
    window_days: int = Query(default=30, ge=1, le=365),
) -> FeedbackInsights:
    """Aggregate feedback for knowledge curators (`knowledge:review`):
    rating totals, a confidence-calibration signal, and clusters of
    negatively-rated questions.

    Everything returned is raw signal or an automated, UNREVIEWED pattern --
    not a finding that any answer was wrong. Question text is redacted and
    truncated and never attributed to a user.
    """
    return await feedback_service.get_insights(session, actor, window_days=window_days)
