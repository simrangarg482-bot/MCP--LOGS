"""`app.evaluation.feedback_candidates` -- user feedback must never become
trusted evaluation data without a named human approving it."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agents.feedback.schemas import FeedbackPattern
from app.evaluation import feedback_candidates as fc

_NOW = datetime(2026, 11, 28, tzinfo=UTC)


def _pattern(**overrides) -> FeedbackPattern:
    values = dict(
        representative_query="How do I reset the VPN token?",
        execution_ids=[uuid.uuid4(), uuid.uuid4()],
        not_helpful_count=2,
        avg_confidence=0.6,
        no_evidence_share=0.5,
        top_reason_codes=["outdated"],
    )
    values.update(overrides)
    return FeedbackPattern(**values)


def _approved(candidate: fc.FeedbackCandidate, **overrides) -> fc.FeedbackCandidate:
    values = candidate.model_dump()
    values.update(
        status="approved",
        reviewer_id="curator-1",
        reviewed_at=_NOW,
        category="clear-answer",
        expected_route="answer",
        expected_answer="Rotate it in the admin console.",
        rationale="Stated plainly in the VPN runbook.",
        evidence=["Rotate the token in the admin console."],
    )
    values.update(overrides)
    return fc.FeedbackCandidate(**values)


def test_new_candidates_are_proposed_and_carry_no_expected_answer() -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])

    assert candidate.status == "proposed"
    assert candidate.reviewer_id is None and candidate.reviewed_at is None
    assert candidate.expected_answer is None and candidate.expected_route is None
    assert candidate.question == "How do I reset the VPN token?"


def test_candidate_ids_are_stable_for_the_same_executions() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]

    first = fc.candidate_id(_pattern(execution_ids=ids))
    second = fc.candidate_id(_pattern(execution_ids=list(reversed(ids))))

    assert first == second
    assert fc.candidate_id(_pattern()) != first


@pytest.mark.parametrize(
    "missing",
    ["reviewer_id", "reviewed_at", "category", "expected_route", "expected_answer", "rationale", "evidence"],
)
def test_an_approved_candidate_must_be_fully_reviewed(missing) -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])
    empty = [] if missing == "evidence" else None

    with pytest.raises(ValidationError):
        _approved(candidate, **{missing: empty})


def test_a_proposed_candidate_cannot_carry_reviewer_fields() -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])

    with pytest.raises(ValidationError):
        fc.FeedbackCandidate(**{**candidate.model_dump(), "reviewer_id": "me"})


def test_only_approved_candidates_reach_the_dataset_shape() -> None:
    proposed, to_approve, to_reject = (
        fc.candidates_from_patterns([_pattern(execution_ids=[uuid.uuid4(), uuid.uuid4()])])[0]
        for _ in range(3)
    )
    approved = _approved(to_approve)
    rejected = fc.FeedbackCandidate(
        **{**to_reject.model_dump(), "status": "rejected", "reviewer_id": "curator-1", "reviewed_at": _NOW}
    )
    candidate_file = fc.new_candidate_file([proposed, approved, rejected])

    questions = fc.approved_confidence_questions(candidate_file)

    assert [q["id"] for q in questions] == [approved.id]
    assert set(questions[0]) == {
        "id", "category", "collection", "question", "expected_answer",
        "expected_route", "rationale", "evidence",
    }  # exactly the shape scripts/eval_confidence_dataset.json uses


def test_the_shipped_dataset_shape_matches_what_promotion_emits() -> None:
    from pathlib import Path

    dataset = json.loads(
        (Path(__file__).resolve().parents[2] / "scripts" / "eval_confidence_dataset.json").read_text(
            encoding="utf-8"
        )
    )
    (candidate,) = fc.candidates_from_patterns([_pattern()])
    (question,) = fc.approved_confidence_questions(fc.new_candidate_file([_approved(candidate)]))

    assert set(question) == set(dataset["questions"][0])


def test_promotion_adds_only_new_approved_ids_and_never_edits_existing_entries() -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])
    approved = _approved(candidate)
    dataset = {"_comment": ["x"], "questions": [{"id": "existing", "question": "keep me"}]}
    candidate_file = fc.new_candidate_file([approved])

    dataset, added = fc.promote_into_dataset(dataset, candidate_file)
    dataset, added_again = fc.promote_into_dataset(dataset, candidate_file)

    assert (added, added_again) == (1, 0)
    assert [q["id"] for q in dataset["questions"]] == ["existing", approved.id]
    assert dataset["questions"][0] == {"id": "existing", "question": "keep me"}


def test_promotion_ignores_a_file_of_only_unreviewed_candidates() -> None:
    candidates = fc.candidates_from_patterns([_pattern(), _pattern()])
    dataset = {"questions": []}

    dataset, added = fc.promote_into_dataset(dataset, fc.new_candidate_file(candidates))

    assert added == 0 and dataset["questions"] == []


def test_re_export_never_overwrites_a_reviewed_candidate() -> None:
    (fresh,) = fc.candidates_from_patterns([_pattern(execution_ids=[uuid.UUID(int=1), uuid.UUID(int=2)])])
    reviewed = _approved(fresh, review_note="checked against the runbook")
    new_one = fc.candidates_from_patterns([_pattern(execution_ids=[uuid.UUID(int=3), uuid.UUID(int=4)])])[0]

    merged = fc.merge_candidates([reviewed], [fresh, new_one])

    assert [c.id for c in merged] == [reviewed.id, new_one.id]
    assert merged[0].status == "approved" and merged[0].review_note == "checked against the runbook"


def test_candidate_file_round_trips_through_disk(tmp_path) -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])
    path = tmp_path / "candidates.json"

    fc.save_candidate_file(path, fc.new_candidate_file([_approved(candidate)]))
    loaded = fc.load_candidate_file(path)

    assert loaded.candidates[0].status == "approved"
    assert loaded.schema_version == "feedback-candidates-v1"


def test_a_hand_edited_file_that_approves_without_review_fails_to_load(tmp_path) -> None:
    (candidate,) = fc.candidates_from_patterns([_pattern()])
    path = tmp_path / "candidates.json"
    fc.save_candidate_file(path, fc.new_candidate_file([candidate]))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["candidates"][0]["status"] = "approved"  # flipped by hand, nothing else filled in
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValidationError):
        fc.load_candidate_file(path)
