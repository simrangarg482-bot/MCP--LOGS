"""Evaluation-dataset candidates derived from user feedback -- the human gate.

WHY THIS IS A FILE, NOT A TABLE
    Every other piece of evaluation data in this repository is
    version-controlled and reviewed like code (see
    `app.evaluation.semantic.annotation_store`'s "why a JSON fixture file, not
    a database table"). Candidates follow the same convention: an exported
    JSON file a person edits and a pull request reviews. No new table, no new
    authorization surface, no background job.

THE FOUR LAYERS, AND WHERE THIS SITS
    1. raw user feedback        -- `answer_feedback` rows (an opinion)
    2. automated patterns       -- `agents.feedback.patterns` (a lead)
    3. CURATOR-REVIEWED CASES   -- a `FeedbackCandidate` a named human moved to
                                   `approved` / `rejected`   <- this module
    4. approved dataset entries -- `approved_confidence_questions`, the ONLY
                                   path into `scripts/eval_confidence_dataset
                                   .json`

    A candidate is born `proposed` and carries only what feedback can honestly
    supply: a redacted question and some counts. It does NOT carry an expected
    answer, a category or a route -- feedback cannot know the right answer, and
    pretending it can would turn "users were unhappy" into "this is the truth".
    Those fields exist only after a reviewer fills them in, and `approved`
    without them (or without a reviewer's name and time) is rejected by
    validation, so a half-reviewed candidate can never leak into a dataset.

NO ANSWER TEXT. Neither the answer nor any citation excerpt is exported
(they are not even stored). A reviewer re-asks the question against the live
corpus and writes the expected answer and its verbatim evidence themselves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.feedback.schemas import FeedbackPattern

SCHEMA_VERSION = "feedback-candidates-v1"

CandidateStatus = Literal["proposed", "approved", "rejected"]
#: The three categories `scripts/eval_confidence.py` distinguishes.
CandidateCategory = Literal["clear-answer", "ambiguous", "no-information"]
ExpectedRoute = Literal["answer", "investigation"]

_DEFAULT_COLLECTION = "documentation+conversations"


class FeedbackCandidate(BaseModel):
    """One candidate evaluation question, and its review state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: Redacted, truncated question text (never attributed to a user).
    question: str
    #: Automated signal -- context for the reviewer, never ground truth.
    not_helpful_count: int
    avg_confidence: float | None = None
    no_evidence_share: float | None = None
    top_reason_codes: list[str] = Field(default_factory=list)

    status: CandidateStatus = "proposed"
    # Filled by the reviewer.
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    category: CandidateCategory | None = None
    expected_route: ExpectedRoute | None = None
    expected_answer: str | None = None
    rationale: str | None = None
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _review_state_is_consistent(self) -> FeedbackCandidate:
        if self.status == "proposed":
            if self.reviewer_id or self.reviewed_at:
                raise ValueError("a proposed candidate must not carry reviewer fields")
            return self
        if not self.reviewer_id or not self.reviewed_at:
            raise ValueError(f"a {self.status} candidate needs reviewer_id and reviewed_at")
        if self.status == "approved":
            missing = [
                name
                for name, value in (
                    ("category", self.category),
                    ("expected_route", self.expected_route),
                    ("expected_answer", (self.expected_answer or "").strip()),
                    ("rationale", (self.rationale or "").strip()),
                    ("evidence", self.evidence),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"an approved candidate needs: {', '.join(missing)}")
        return self


class CandidateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["feedback-candidates-v1"] = SCHEMA_VERSION
    generated_at: datetime
    candidates: list[FeedbackCandidate] = Field(default_factory=list)


def candidate_id(pattern: FeedbackPattern) -> str:
    """Stable across runs for the same set of executions, so re-exporting
    never duplicates a candidate or resets its review."""
    digest = hashlib.sha256(
        ",".join(sorted(str(execution_id) for execution_id in pattern.execution_ids)).encode()
    ).hexdigest()
    return f"fb-{digest[:12]}"


def candidates_from_patterns(patterns: Sequence[FeedbackPattern]) -> list[FeedbackCandidate]:
    return [
        FeedbackCandidate(
            id=candidate_id(pattern),
            question=pattern.representative_query,
            not_helpful_count=pattern.not_helpful_count,
            avg_confidence=pattern.avg_confidence,
            no_evidence_share=pattern.no_evidence_share,
            top_reason_codes=list(pattern.top_reason_codes),
        )
        for pattern in patterns
    ]


def merge_candidates(
    existing: Sequence[FeedbackCandidate], fresh: Sequence[FeedbackCandidate]
) -> list[FeedbackCandidate]:
    """Add candidates not already present. An existing candidate is NEVER
    overwritten -- its review (or a reviewer's edits) must survive a re-export."""
    known = {candidate.id for candidate in existing}
    return [*existing, *(candidate for candidate in fresh if candidate.id not in known)]


def load_candidate_file(path: str | Path) -> CandidateFile:
    return CandidateFile.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def save_candidate_file(path: str | Path, candidate_file: CandidateFile) -> None:
    Path(path).write_text(
        json.dumps(candidate_file.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def new_candidate_file(candidates: Sequence[FeedbackCandidate]) -> CandidateFile:
    return CandidateFile(generated_at=datetime.now(UTC), candidates=list(candidates))


def approved_confidence_questions(candidate_file: CandidateFile) -> list[dict]:
    """The reviewed-and-approved candidates, in the exact question shape of
    `scripts/eval_confidence_dataset.json`. `proposed` and `rejected`
    candidates are never returned -- this function is the only gate."""
    questions: list[dict] = []
    for candidate in candidate_file.candidates:
        if candidate.status != "approved":
            continue
        questions.append(
            {
                "id": candidate.id,
                "category": candidate.category,
                "collection": _DEFAULT_COLLECTION,
                "question": candidate.question,
                "expected_answer": candidate.expected_answer,
                "expected_route": candidate.expected_route,
                "rationale": candidate.rationale,
                "evidence": list(candidate.evidence),
            }
        )
    return questions


def promote_into_dataset(dataset: dict, candidate_file: CandidateFile) -> tuple[dict, int]:
    """Append approved questions whose id is not already in `dataset`;
    returns `(dataset, number_added)`. Existing entries are never modified."""
    present = {question["id"] for question in dataset["questions"]}
    added = [q for q in approved_confidence_questions(candidate_file) if q["id"] not in present]
    dataset["questions"].extend(added)
    return dataset, len(added)
