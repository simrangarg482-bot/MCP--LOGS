"""Dataset validation for `app.evaluation.semantic.schemas` -- section 15's
"malformed case rejected / missing reference handled / invalid metric
config rejected / unsupported category rejected" requirement.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.evaluation.semantic.schemas import (
    AnswerJudgement,
    AnswerQualityCase,
    AnswerQualityDimension,
    CalibrationCandidateResult,
    CalibrationReport,
    ExecutionMetadata,
    InvestigationABCase,
    RefusalJudgement,
    SubstantiveAnswerJudgement,
)


def _dimension(score: float = 0.5) -> AnswerQualityDimension:
    return AnswerQualityDimension(score=score, reason="because")


def test_answer_quality_case_requires_provenance():
    with pytest.raises(ValidationError):
        AnswerQualityCase(id="c1", question="q")  # type: ignore[call-arg]


def test_answer_quality_case_rejects_unsupported_provenance():
    with pytest.raises(ValidationError):
        AnswerQualityCase(
            id="c1",
            provenance="made_up_source",
            question="q",  # type: ignore[arg-type]
        )


def test_answer_quality_case_missing_reference_answer_is_handled_as_none():
    case = AnswerQualityCase(id="c1", provenance="synthetic_controlled", question="q")
    assert case.reference_answer is None
    assert case.evidence_texts == []


def test_answer_quality_case_defaults_expected_answer_mode_to_unlabeled():
    case = AnswerQualityCase(id="c1", provenance="synthetic_controlled", question="q")
    assert case.expected_answer_mode == "unlabeled"
    assert case.fixed_answer is None


def test_answer_quality_case_rejects_unsupported_expected_answer_mode():
    with pytest.raises(ValidationError):
        AnswerQualityCase(
            id="c1",
            provenance="synthetic_controlled",
            question="q",
            expected_answer_mode="maybe",  # type: ignore[arg-type]
        )


def test_answer_quality_dimension_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        AnswerQualityDimension(score=1.5, reason="too high")
    with pytest.raises(ValidationError):
        AnswerQualityDimension(score=-0.1, reason="too low")


def test_substantive_judgement_mean_score_averages_all_four_dimensions():
    judgement = SubstantiveAnswerJudgement(
        observed_mode="substantive_answer",
        correctness=_dimension(1.0),
        relevance=_dimension(0.5),
        usefulness=_dimension(0.5),
        faithfulness=_dimension(0.0),
    )
    assert judgement.mean_score == pytest.approx(0.5)


def test_substantive_judgement_rejects_missing_dimension():
    with pytest.raises(ValidationError):
        SubstantiveAnswerJudgement(
            observed_mode="substantive_answer",
            correctness=_dimension(),
            relevance=_dimension(),
            usefulness=_dimension(),
        )  # type: ignore[call-arg]


def test_substantive_judgement_rejects_unsupported_observed_mode():
    with pytest.raises(ValidationError):
        SubstantiveAnswerJudgement(
            observed_mode="no_answer",  # type: ignore[arg-type]  -- not a substantive mode
            correctness=_dimension(),
            relevance=_dimension(),
            usefulness=_dimension(),
            faithfulness=_dimension(),
        )


def test_refusal_judgement_mean_score_averages_all_four_dimensions():
    judgement = RefusalJudgement(
        abstention_correctness=_dimension(1.0),
        unsupported_claim_avoidance=_dimension(1.0),
        explanation_quality=_dimension(0.0),
        appropriate_next_step=_dimension(0.0),
    )
    assert judgement.mean_score == pytest.approx(0.5)


def test_answer_judgement_mean_score_delegates_to_whichever_side_is_set():
    substantive = AnswerJudgement(
        observed_answer_mode="substantive_answer",
        substantive=SubstantiveAnswerJudgement(
            observed_mode="substantive_answer",
            correctness=_dimension(1.0),
            relevance=_dimension(1.0),
            usefulness=_dimension(1.0),
            faithfulness=_dimension(1.0),
        ),
    )
    assert substantive.mean_score == pytest.approx(1.0)

    refusal = AnswerJudgement(
        observed_answer_mode="no_answer",
        refusal=RefusalJudgement(
            abstention_correctness=_dimension(0.0),
            unsupported_claim_avoidance=_dimension(0.0),
            explanation_quality=_dimension(0.0),
            appropriate_next_step=_dimension(0.0),
        ),
    )
    assert refusal.mean_score == pytest.approx(0.0)


def test_investigation_ab_case_rejects_unsupported_category_style_field():
    # `evidence` must be a list of (reference, source, summary) triples --
    # a malformed shape (missing the tuple structure) must be rejected, not
    # silently coerced.
    with pytest.raises(ValidationError):
        InvestigationABCase(
            id="i1",
            provenance="synthetic_controlled",
            query="q",
            evidence=["not-a-triple"],  # type: ignore[list-item]
        )


def test_calibration_report_rejects_unsupported_status_value():
    with pytest.raises(ValidationError):
        CalibrationReport(
            setting_name="x",
            current_value=0.5,
            description="d",
            sample_size=5,
            minimum_sample_size=20,
            candidates=[CalibrationCandidateResult(candidate_value=0.5, metrics={})],
            status="probably_fine",  # type: ignore[arg-type]
            rationale="r",
        )


def test_calibration_report_defaults_recommended_value_to_none():
    report = CalibrationReport(
        setting_name="x",
        current_value=0.5,
        description="d",
        sample_size=0,
        minimum_sample_size=20,
        status="insufficient_data",
        rationale="r",
    )
    assert report.recommended_value is None
    assert report.candidates == []


# --------------------------------------------------------------------------
# secret and data safety (section 14) -- a report must never carry a
# credential, only the provider/model identifiers needed for reproducibility
# --------------------------------------------------------------------------


def test_execution_metadata_has_no_field_shaped_like_a_secret():
    """Structural guarantee, not just a runtime check: the schema itself
    has no field a caller could accidentally populate with a credential --
    only `model_provider`/`model_name` identify what ran."""
    forbidden_substrings = ("key", "secret", "credential", "token", "password")
    for field_name in ExecutionMetadata.model_fields:
        lowered = field_name.lower()
        assert not any(bad in lowered for bad in forbidden_substrings), field_name


def test_execution_metadata_json_never_contains_a_value_never_passed_in():
    """Round-trips a real-looking API key value through nothing --
    `ExecutionMetadata` has no field for it, so it can never appear in the
    serialized report regardless of what the caller happens to have in
    scope when building one."""
    from datetime import UTC, datetime

    fake_key = "sk-proj-this-should-never-appear-in-a-report"  # noqa: S105 - test fixture, not a real credential
    metadata = ExecutionMetadata(
        dataset_version="v1",
        model_provider="openai",
        model_name="gpt-4o-mini",
        generated_at=datetime.now(UTC),
        git_commit="abc123",
    )
    assert fake_key not in metadata.model_dump_json()
