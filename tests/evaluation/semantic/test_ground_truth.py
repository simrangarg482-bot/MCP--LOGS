"""Tests for `app.evaluation.semantic.ground_truth` -- Priority 11 section
7's independent-review/disagreement state machine and section 8's
inter-annotator agreement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.evaluation.semantic import ground_truth
from app.evaluation.semantic.schemas import (
    AnnotationDecision,
    AnswerQualityCase,
    ResolutionAnnotation,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _case(expected_answer_mode: str = "no_answer") -> AnswerQualityCase:
    return AnswerQualityCase(
        id="case-1",
        provenance="synthetic_controlled",
        question="q",
        evidence_texts=["e"],
        expected_answer_mode=expected_answer_mode,
        fixed_answer="a",
    )


def _annotation(
    reviewer_id: str, observed_mode: str, *, offset_seconds: int = 0
) -> AnnotationDecision:
    return AnnotationDecision(
        case_id="case-1",
        dataset_version="v1",
        case_snapshot_hash="h",
        reviewer_id=reviewer_id,
        provenance="synthetic_controlled_annotation",
        annotated_at=_T0 + timedelta(seconds=offset_seconds),
        observed_mode=observed_mode,
        rationale="because",
    )


def _resolution(observed_mode: str, resolved_ids: list[str]) -> ResolutionAnnotation:
    return ResolutionAnnotation(
        case_id="case-1",
        dataset_version="v1",
        case_snapshot_hash="h",
        reviewer_id="resolver",
        provenance="synthetic_controlled_annotation",
        annotated_at=_T0 + timedelta(seconds=100),
        observed_mode=observed_mode,
        rationale="resolved",
        resolved_annotation_ids=resolved_ids,
    )


# --------------------------------------------------------------------------
# derive_outcome_for_annotation
# --------------------------------------------------------------------------


def test_derive_outcome_matches_the_automated_evaluator_decision_rule():
    assert ground_truth.derive_outcome_for_annotation("answer", "substantive_answer") == "correct"
    assert (
        ground_truth.derive_outcome_for_annotation("no_answer", "substantive_answer")
        == "critical_failure"
    )


def test_derive_outcome_is_none_for_unlabeled_expected_mode():
    assert ground_truth.derive_outcome_for_annotation("unlabeled", "no_answer") is None


# --------------------------------------------------------------------------
# resolve_ground_truth -- the four states
# --------------------------------------------------------------------------


def test_zero_annotations_raises():
    with pytest.raises(ValueError, match="zero annotations"):
        ground_truth.resolve_ground_truth(_case(), [])


def test_single_review_state_is_represented_honestly():
    result = ground_truth.resolve_ground_truth(_case(), [_annotation("r1", "no_answer")])
    assert result.status == "single_review"
    assert result.final_observed_mode == "no_answer"
    assert result.final_outcome == "correct"


def test_agreed_review_state():
    anns = [_annotation("r1", "no_answer"), _annotation("r2", "no_answer", offset_seconds=1)]
    result = ground_truth.resolve_ground_truth(_case(), anns)
    assert result.status == "agreed_review"
    assert result.final_observed_mode == "no_answer"


def test_unresolved_disagreement_state_has_no_final_label():
    anns = [
        _annotation("r1", "no_answer"),
        _annotation("r2", "substantive_answer", offset_seconds=1),
    ]
    result = ground_truth.resolve_ground_truth(_case(), anns)
    assert result.status == "unresolved_disagreement"
    assert result.final_observed_mode is None
    assert result.final_outcome is None
    # Both original annotations are preserved, not discarded.
    assert len(result.annotations) == 2


def test_resolved_disagreement_state_uses_the_resolution_not_either_original():
    anns = [
        _annotation("r1", "no_answer"),
        _annotation("r2", "substantive_answer", offset_seconds=1),
    ]
    resolution = _resolution("substantive_answer", ["r1:x", "r2:y"])
    result = ground_truth.resolve_ground_truth(_case(), anns, resolution)
    assert result.status == "resolved_disagreement"
    assert result.final_observed_mode == "substantive_answer"
    # Original disagreeing annotations are still there, unmodified.
    assert {a.observed_mode for a in result.annotations} == {"no_answer", "substantive_answer"}
    assert result.resolution is resolution


def test_third_annotation_does_not_change_the_independent_pairs_verdict():
    """Documented simplification (see module docstring): only the first two
    chronologically count as the independent pair."""
    anns = [
        _annotation("r1", "no_answer"),
        _annotation("r2", "no_answer", offset_seconds=1),
        _annotation("r3", "substantive_answer", offset_seconds=2),
    ]
    result = ground_truth.resolve_ground_truth(_case(), anns)
    assert result.status == "agreed_review"  # r1/r2 agree; r3 doesn't change that


# --------------------------------------------------------------------------
# compute_inter_annotator_agreement
# --------------------------------------------------------------------------


def test_agreement_below_floor_is_insufficient_data():
    cases = {"case-1": _case()}
    annotations = {
        "case-1": [_annotation("r1", "no_answer"), _annotation("r2", "no_answer", offset_seconds=1)]
    }
    report = ground_truth.compute_inter_annotator_agreement(
        cases,
        annotations,
        dataset_version="v1",
        annotation_schema_version="annotation-v1",
        minimum_sample_size=20,
    )
    assert report.status == "insufficient_data"
    assert report.double_reviewed_case_count == 1
    assert report.cohens_kappa is None


def test_agreement_counts_agreed_and_disagreed_correctly():
    cases = {
        "case-1": AnswerQualityCase(
            id="case-1", provenance="synthetic_controlled", question="q", fixed_answer="a"
        ),
        "case-2": AnswerQualityCase(
            id="case-2", provenance="synthetic_controlled", question="q", fixed_answer="a"
        ),
    }
    annotations = {
        "case-1": [
            AnnotationDecision(
                case_id="case-1",
                dataset_version="v1",
                case_snapshot_hash="h",
                reviewer_id="r1",
                provenance="synthetic_controlled_annotation",
                annotated_at=_T0,
                observed_mode="no_answer",
                rationale="x",
            ),
            AnnotationDecision(
                case_id="case-1",
                dataset_version="v1",
                case_snapshot_hash="h",
                reviewer_id="r2",
                provenance="synthetic_controlled_annotation",
                annotated_at=_T0 + timedelta(seconds=1),
                observed_mode="no_answer",
                rationale="x",
            ),
        ],
        "case-2": [
            AnnotationDecision(
                case_id="case-2",
                dataset_version="v1",
                case_snapshot_hash="h",
                reviewer_id="r1",
                provenance="synthetic_controlled_annotation",
                annotated_at=_T0,
                observed_mode="no_answer",
                rationale="x",
            ),
            AnnotationDecision(
                case_id="case-2",
                dataset_version="v1",
                case_snapshot_hash="h",
                reviewer_id="r2",
                provenance="synthetic_controlled_annotation",
                annotated_at=_T0 + timedelta(seconds=1),
                observed_mode="substantive_answer",
                rationale="x",
            ),
        ],
    }
    report = ground_truth.compute_inter_annotator_agreement(
        cases,
        annotations,
        dataset_version="v1",
        annotation_schema_version="annotation-v1",
        minimum_sample_size=2,
    )
    assert report.double_reviewed_case_count == 2
    assert report.agreed_case_count == 1
    assert report.disagreed_case_count == 1
    assert report.unresolved_disagreement_count == 1
    assert report.raw_agreement_rate == pytest.approx(0.5)
    assert report.status == "computed"


def test_resolved_disagreement_is_not_double_counted_as_unresolved():
    """The bug this test locks in: a resolution must be consulted, or a
    genuinely resolved disagreement is miscounted as unresolved."""
    cases = {
        "case-1": AnswerQualityCase(
            id="case-1", provenance="synthetic_controlled", question="q", fixed_answer="a"
        )
    }
    r1 = AnnotationDecision(
        case_id="case-1",
        dataset_version="v1",
        case_snapshot_hash="h",
        reviewer_id="r1",
        provenance="synthetic_controlled_annotation",
        annotated_at=_T0,
        observed_mode="no_answer",
        rationale="x",
    )
    r2 = AnnotationDecision(
        case_id="case-1",
        dataset_version="v1",
        case_snapshot_hash="h",
        reviewer_id="r2",
        provenance="synthetic_controlled_annotation",
        annotated_at=_T0 + timedelta(seconds=1),
        observed_mode="substantive_answer",
        rationale="x",
    )
    annotations = {"case-1": [r1, r2]}
    resolution = _resolution(
        "no_answer",
        [
            f"{r1.reviewer_id}:{r1.annotated_at.isoformat()}",
            f"{r2.reviewer_id}:{r2.annotated_at.isoformat()}",
        ],
    )

    report_without_resolution = ground_truth.compute_inter_annotator_agreement(
        cases,
        annotations,
        dataset_version="v1",
        annotation_schema_version="annotation-v1",
        minimum_sample_size=1,
    )
    assert report_without_resolution.unresolved_disagreement_count == 1

    report_with_resolution = ground_truth.compute_inter_annotator_agreement(
        cases,
        annotations,
        dataset_version="v1",
        annotation_schema_version="annotation-v1",
        minimum_sample_size=1,
        resolutions_by_case={"case-1": resolution},
    )
    assert report_with_resolution.unresolved_disagreement_count == 0
    assert report_with_resolution.disagreed_case_count == 1  # still a disagreement, just resolved


def test_cohens_kappa_is_none_when_expected_agreement_is_certain():
    """Both raters always chose the same single label -- p_expected == 1.0,
    kappa is mathematically undefined, not silently reported as 1.0."""
    kappa = ground_truth._cohens_kappa([("no_answer", "no_answer")] * 5)
    assert kappa is None


def test_cohens_kappa_is_computed_for_genuine_variation():
    pairs = [
        ("no_answer", "no_answer"),
        ("no_answer", "no_answer"),
        ("substantive_answer", "substantive_answer"),
        ("substantive_answer", "no_answer"),
    ]
    kappa = ground_truth._cohens_kappa(pairs)
    assert kappa is not None
    assert -1.0 <= kappa <= 1.0
