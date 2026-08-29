"""Tests for `app.evaluation.semantic.evaluator_validation` -- Priority 11
section 9's deterministic evaluator-vs-human comparison and section 10's
severity model. No LLM call anywhere in this module; every test here
constructs inputs directly.
"""

from __future__ import annotations

from app.evaluation.semantic import evaluator_validation
from app.evaluation.semantic.schemas import (
    AnswerJudgement,
    AnswerQualityResult,
    CaseGroundTruth,
    SubstantiveAnswerJudgement,
)


def _dim(score: float = 0.9) -> dict:
    from app.evaluation.semantic.schemas import AnswerQualityDimension

    return AnswerQualityDimension(score=score, reason="r")


def _result(
    case_id: str, observed_mode: str, outcome: str | None, *, error: str | None = None
) -> AnswerQualityResult:
    judgement = None
    if error is None:
        judgement = AnswerJudgement(
            observed_answer_mode=observed_mode,
            substantive=SubstantiveAnswerJudgement(
                observed_mode=observed_mode
                if observed_mode != "no_answer"
                else "substantive_answer",
                correctness=_dim(),
                relevance=_dim(),
                usefulness=_dim(),
                faithfulness=_dim(),
            )
            if observed_mode != "no_answer"
            else None,
        )
    return AnswerQualityResult(
        case_id=case_id,
        question="q",
        generated_answer="a",
        expected_answer_mode="answer",
        judgement=judgement,
        outcome_correctness=outcome,
        error=error,
    )


def _ground_truth(
    case_id: str, observed_mode: str, outcome: str, *, status: str = "agreed_review"
) -> CaseGroundTruth:
    return CaseGroundTruth(
        case_id=case_id,
        dataset_version="v1",
        annotations=[],
        status=status,
        final_observed_mode=observed_mode if status != "unresolved_disagreement" else None,
        final_outcome=outcome if status != "unresolved_disagreement" else None,
    )


def test_deterministic_comparison_counts_agreement():
    results = {"c1": _result("c1", "substantive_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "correct")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.sample_size == 1
    assert report.answer_mode_agreement_rate == 1.0
    assert report.outcome_agreement_rate == 1.0
    assert report.status == "computed"


def test_disagreement_is_counted_and_shown_in_confusion_matrix():
    results = {"c1": _result("c1", "substantive_answer", "overconfident")}
    truths = {"c1": _ground_truth("c1", "qualified_answer", "correct")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.outcome_agreement_rate == 0.0
    assert report.outcome_confusion_matrix[0].human_label == "correct"
    assert report.outcome_confusion_matrix[0].evaluator_label == "overconfident"
    assert report.outcome_confusion_matrix[0].count == 1


def test_unresolved_disagreement_is_excluded_never_compared():
    results = {"c1": _result("c1", "substantive_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "no_answer", "correct", status="unresolved_disagreement")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.sample_size == 0
    assert report.excluded_case_count == 1


def test_case_with_evaluator_error_is_excluded_not_compared():
    results = {"c1": _result("c1", "substantive_answer", None, error="boom")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "correct")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.sample_size == 0
    assert report.excluded_case_count == 1


def test_insufficient_sample_never_claims_validity():
    results = {"c1": _result("c1", "substantive_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "correct")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=20
    )
    assert report.status == "insufficient_data"
    # The measurement is still reported, just not claimed as meaningful.
    assert report.outcome_agreement_rate == 1.0


def test_confusion_matrix_correct_across_multiple_cases():
    results = {
        "c1": _result("c1", "substantive_answer", "correct"),
        "c2": _result("c2", "substantive_answer", "critical_failure"),
        "c3": _result("c3", "no_answer", "correct"),
    }
    truths = {
        "c1": _ground_truth("c1", "substantive_answer", "correct"),
        "c2": _ground_truth("c2", "no_answer", "correct"),  # human: correct refusal expected
        "c3": _ground_truth("c3", "no_answer", "correct"),
    }
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.sample_size == 3
    cells = {(c.human_label, c.evaluator_label): c.count for c in report.outcome_confusion_matrix}
    assert cells[("correct", "correct")] == 2
    assert cells[("correct", "critical_failure")] == 1


def test_per_class_precision_recall_f1():
    # 2 human "correct", evaluator gets 1 right and 1 wrong (calls it overconfident).
    results = {
        "c1": _result("c1", "substantive_answer", "correct"),
        "c2": _result("c2", "substantive_answer", "overconfident"),
    }
    truths = {
        "c1": _ground_truth("c1", "substantive_answer", "correct"),
        "c2": _ground_truth("c2", "substantive_answer", "correct"),
    }
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    correct_metric = next(m for m in report.outcome_class_metrics if m.label == "correct")
    assert correct_metric.support == 2
    assert correct_metric.recall == 0.5  # only 1 of 2 "correct" cases caught
    assert correct_metric.precision == 1.0  # every evaluator "correct" call was right


# --------------------------------------------------------------------------
# severity model (section 10)
# --------------------------------------------------------------------------


def test_hallucination_normalized_as_correct_is_critical_severity():
    results = {"c1": _result("c1", "substantive_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "no_answer", "critical_failure")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert len(report.severe_disagreements) == 1
    assert report.severe_disagreements[0].severity == "critical"


def test_lazy_refusal_called_correct_is_critical_severity():
    results = {"c1": _result("c1", "no_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "incorrect_refusal")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert len(report.severe_disagreements) == 1
    assert report.severe_disagreements[0].severity == "critical"


def test_missed_overconfidence_is_high_not_critical_severity():
    results = {"c1": _result("c1", "substantive_answer", "correct")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "overconfident")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert len(report.severe_disagreements) == 1
    assert report.severe_disagreements[0].severity == "high"


def test_lower_severity_mismatch_is_not_flagged_severe():
    """Section 10's own explicit example: human=correct, evaluator=
    partially_correct is 'lower severity' -- not in `severe_disagreements`
    at all, still visible in the confusion matrix."""
    results = {"c1": _result("c1", "substantive_answer", "partially_correct")}
    truths = {"c1": _ground_truth("c1", "substantive_answer", "correct")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.severe_disagreements == []
    assert len(report.outcome_confusion_matrix) == 1  # still recorded


def test_agreement_is_never_flagged_as_a_disagreement():
    results = {"c1": _result("c1", "no_answer", "critical_failure")}
    truths = {"c1": _ground_truth("c1", "no_answer", "critical_failure")}
    report = evaluator_validation.validate_evaluator_against_ground_truth(
        results, truths, dataset_version="v1", minimum_sample_size=1
    )
    assert report.severe_disagreements == []
