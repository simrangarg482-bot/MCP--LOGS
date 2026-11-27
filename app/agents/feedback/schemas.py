"""Pydantic contracts for `agents.feedback`.

Request bodies carry NO `organization_id`, `user_id` or `execution_id`: the
organization and user come from the authenticated `Identity`, the execution
from the URL path and is re-validated server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FeedbackRating = Literal["helpful", "not_helpful"]

#: Why an answer was not helpful. Optional and only meaningful for
#: `not_helpful`. A closed, small set on purpose: structured reasons are what
#: let patterns be found without an LLM reading free text.
ReasonCode = Literal[
    "incorrect",  # the answer states something wrong
    "incomplete",  # right direction, missing what I needed
    "outdated",  # was true once, no longer
    "irrelevant_sources",  # cited sources do not match my question
    "no_answer_needed_more",  # it declined, but the knowledge should exist
    "other",
]

MAX_COMMENT_LENGTH = 2000


class FeedbackSubmit(BaseModel):
    """Body of `PUT /ask/executions/{execution_id}/feedback`."""

    model_config = ConfigDict(extra="forbid")

    rating: FeedbackRating
    reason_code: ReasonCode | None = None
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_LENGTH)

    @field_validator("comment")
    @classmethod
    def _blank_comment_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class Feedback(BaseModel):
    """One user's rating of one execution, as read back."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    execution_id: uuid.UUID
    rating: FeedbackRating
    reason_code: ReasonCode | None
    comment: str | None
    created_at: datetime
    updated_at: datetime


# --- curator-facing, aggregate, read-only ---------------------------------


class RatingTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    helpful: int
    not_helpful: int
    #: not_helpful counts by reason code ("unspecified" when none was given).
    not_helpful_by_reason: dict[str, int]


class CalibrationBucketSummary(BaseModel):
    """Helpful rate within one confidence band. A signal for confidence
    calibration, NOT a calibration: users rate usefulness, not correctness,
    and small buckets are noise (`count` is exposed so a reader can tell)."""

    model_config = ConfigDict(frozen=True)

    range_low: float
    range_high: float
    count: int
    helpful: int
    not_helpful: int
    helpful_rate: float | None


class FeedbackPattern(BaseModel):
    """A cluster of similar questions that were rated not helpful.

    AUTOMATICALLY IDENTIFIED and UNREVIEWED: `reviewed` is always False here.
    It says "several people were unhappy with answers to questions like this",
    not "the answers were wrong".
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["automated_pattern"] = "automated_pattern"
    reviewed: Literal[False] = False
    #: Redacted, truncated text of one question in the cluster.
    representative_query: str
    execution_ids: list[uuid.UUID]
    not_helpful_count: int
    avg_confidence: float | None
    #: Share of the cluster's executions with zero citations / a "no_answer"
    #: outcome -- a retrieval-gap indicator.
    no_evidence_share: float | None
    top_reason_codes: list[str]


class FeedbackInsights(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_days: int
    totals: RatingTotals
    calibration: list[CalibrationBucketSummary]
    patterns: list[FeedbackPattern]
