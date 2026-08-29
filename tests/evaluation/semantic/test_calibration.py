"""Tests for `app.evaluation.semantic.calibration` -- section 15's
"calibration reporting doesn't claim significance without sufficient
data" requirement is the central thing under test here.
"""

from __future__ import annotations

import json

import pytest

from app.evaluation.semantic.calibration import (
    DEFAULT_MARGIN_FOR_CHANGE,
    DEFAULT_MINIMUM_SAMPLE_SIZE,
    binary_precision_recall,
    calibration_from_eval_confidence_report,
    evaluator_reliability_eligibility,
    fixed_domain_rule,
    sweep_binary_threshold,
)
from app.evaluation.semantic.schemas import (
    AgreementReport,
    ClassMetric,
    ConfusionMatrixCell,
    EvaluatorValidationReport,
    SevereDisagreement,
)


def test_binary_precision_recall_computes_expected_values():
    predictions = [(True, True), (True, False), (False, True), (False, False)]
    metrics = binary_precision_recall(predictions)
    assert metrics["precision"] == pytest.approx(0.5)
    assert metrics["recall"] == pytest.approx(0.5)
    assert metrics["accuracy"] == pytest.approx(0.5)


def test_binary_precision_recall_returns_none_for_zero_denominator_never_a_fake_zero():
    metrics = binary_precision_recall([(False, False)])
    assert metrics["precision"] is None
    assert metrics["recall"] is None
    assert metrics["f1"] is None


def test_sweep_below_minimum_sample_size_is_always_insufficient_data():
    scored = [(0.9, True), (0.2, False)]  # only 2 examples, far below the floor
    report = sweep_binary_threshold(
        setting_name="x",
        current_value=0.5,
        description="d",
        scored_examples=scored,
        candidates=[0.3, 0.5, 0.7],
    )
    assert report.status == "insufficient_data"
    assert report.sample_size == 2
    assert report.recommended_value is None


def test_sweep_never_labels_calibrated_below_the_structural_floor_even_with_perfect_scores():
    """19 examples, perfectly separable -- clean numbers, but still one
    below `DEFAULT_MINIMUM_SAMPLE_SIZE`."""
    scored = [(0.9, True)] * 9 + [(0.1, False)] * 10  # 19 examples
    assert len(scored) < DEFAULT_MINIMUM_SAMPLE_SIZE
    report = sweep_binary_threshold(
        setting_name="x",
        current_value=0.5,
        description="d",
        scored_examples=scored,
        candidates=[0.5],
    )
    assert report.status == "insufficient_data"


def test_sweep_at_minimum_sample_size_with_clean_current_value_is_calibrated():
    scored = [(0.9, True)] * 10 + [(0.1, False)] * (DEFAULT_MINIMUM_SAMPLE_SIZE - 10)
    report = sweep_binary_threshold(
        setting_name="x",
        current_value=0.5,
        description="d",
        scored_examples=scored,
        candidates=[0.5],
    )
    assert report.sample_size == DEFAULT_MINIMUM_SAMPLE_SIZE
    assert report.status == "calibrated"
    assert report.recommended_value == 0.5


def test_sweep_reports_provisional_not_calibrated_when_a_candidate_clearly_wins():
    # current_value=0.9 misses every positive; candidate 0.3 separates cleanly.
    scored = [(0.5, True)] * 12 + [(0.1, False)] * 10
    report = sweep_binary_threshold(
        setting_name="x",
        current_value=0.9,
        description="d",
        scored_examples=scored,
        candidates=[0.3, 0.9],
    )
    assert report.sample_size >= DEFAULT_MINIMUM_SAMPLE_SIZE
    assert report.status == "provisional"  # never "calibrated" off one run
    assert report.recommended_value == 0.3


def test_sweep_within_margin_stays_provisional_and_keeps_current_value():
    """One ambiguous example (score 0.55, positive) separates candidates
    0.5 and 0.6 by a single false negative -- f1 0.952 vs 1.0, a 0.048
    margin, deliberately just under `DEFAULT_MARGIN_FOR_CHANGE`. The
    current default must be kept (not silently swapped for a candidate
    that isn't a clearly better signal on this sample)."""
    assert pytest.approx(0.05) == DEFAULT_MARGIN_FOR_CHANGE
    scored = [(0.9, True)] * 10 + [(0.1, False)] * 9 + [(0.55, True)]
    assert len(scored) == DEFAULT_MINIMUM_SAMPLE_SIZE

    report = sweep_binary_threshold(
        setting_name="x",
        current_value=0.6,
        description="d",
        scored_examples=scored,
        candidates=[0.5, 0.6],
    )

    assert report.status == "provisional"
    assert report.recommended_value == 0.6


def _write_eval_confidence_report(tmp_path, *, current_threshold, sweep_rows, generated_at):
    payload = {
        "generated_at": generated_at,
        "organization": {"slug": "test-org"},
        "current_default_threshold": current_threshold,
        "best_threshold": max(sweep_rows, key=lambda r: r.get("f1") or 0)["threshold"],
        "best_threshold_metrics": max(sweep_rows, key=lambda r: r.get("f1") or 0),
        "threshold_sweep": sweep_rows,
    }
    path = tmp_path / "eval_confidence_report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_calibration_from_missing_report_returns_none(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert calibration_from_eval_confidence_report(missing) is None


def test_calibration_from_report_with_small_confusion_matrix_is_insufficient_data(tmp_path):
    rows = [
        {
            "threshold": 0.6,
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "tp": 10,
            "fp": 2,
            "fn": 2,
            "tn": 0,
        },
    ]
    path = _write_eval_confidence_report(
        tmp_path, current_threshold=0.6, sweep_rows=rows, generated_at="2026-08-14T00:00:00Z"
    )
    report = calibration_from_eval_confidence_report(path, minimum_sample_size=20)
    assert report is not None
    assert report.status == "insufficient_data"
    assert report.sample_size == 14


def test_calibration_from_report_with_zero_negative_examples_is_insufficient_data(tmp_path):
    """Even with a large confusion matrix, zero negative-class examples
    cannot demonstrate the threshold correctly rejects anything."""
    rows = [
        {
            "threshold": 0.6,
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "tp": 30,
            "fp": 0,
            "fn": 0,
            "tn": 0,
        },
    ]
    path = _write_eval_confidence_report(
        tmp_path, current_threshold=0.6, sweep_rows=rows, generated_at="2026-08-14T00:00:00Z"
    )
    report = calibration_from_eval_confidence_report(path, minimum_sample_size=20)
    assert report is not None
    assert report.status == "insufficient_data"
    assert "negative" in report.rationale.lower()


def test_fixed_domain_rule_is_never_calibrated_and_carries_zero_sample_size():
    report = fixed_domain_rule("some_setting", 0.75, "an intentional design constant")
    assert report.status == "intentionally_fixed_domain_rule"
    assert report.sample_size == 0
    assert report.recommended_value == 0.75


# --------------------------------------------------------------------------
# evaluator_reliability_eligibility -- Priority 11 section 14
# --------------------------------------------------------------------------


def _agreement(status: str = "computed") -> AgreementReport:
    return AgreementReport(
        dataset_version="v1",
        annotation_schema_version="annotation-v1",
        reviewed_case_count=9,
        double_reviewed_case_count=4,
        agreed_case_count=3,
        disagreed_case_count=1,
        unresolved_disagreement_count=0,
        raw_agreement_rate=0.75,
        cohens_kappa=None,
        minimum_sample_size=20,
        status=status,
        rationale="r",
    )


def _validation_report(
    *,
    sample_size: int,
    classes: set[str],
    severe: list[SevereDisagreement] | None = None,
) -> EvaluatorValidationReport:
    matrix = [ConfusionMatrixCell(human_label=c, evaluator_label=c, count=1) for c in classes]
    metrics = [ClassMetric(label=c, support=1, precision=1.0, recall=1.0, f1=1.0) for c in classes]
    return EvaluatorValidationReport(
        dataset_version="v1",
        sample_size=sample_size,
        minimum_sample_size=20,
        answer_mode_agreement_rate=1.0,
        outcome_agreement_rate=1.0,
        outcome_confusion_matrix=matrix,
        outcome_class_metrics=metrics,
        severe_disagreements=severe or [],
        excluded_case_count=0,
        status="computed" if sample_size >= 20 else "insufficient_data",
        rationale="r",
    )


def test_eligibility_below_sample_floor_is_insufficient_data():
    report = evaluator_reliability_eligibility(
        agreement_report=_agreement(),
        evaluator_validation_report=_validation_report(sample_size=9, classes={"correct"}),
        minimum_sample_size=20,
    )
    assert report.status == "insufficient_data"
    assert report.setting_name == "semantic_evaluator_reliability"


def test_eligibility_blocked_by_unreliable_inter_annotator_agreement():
    report = evaluator_reliability_eligibility(
        agreement_report=_agreement(status="insufficient_data"),
        evaluator_validation_report=_validation_report(
            sample_size=25, classes={"correct", "critical_failure", "incorrect_refusal"}
        ),
        minimum_sample_size=20,
    )
    assert report.status == "insufficient_agreement"


def test_eligibility_blocked_by_missing_class_coverage():
    report = evaluator_reliability_eligibility(
        agreement_report=_agreement(),
        evaluator_validation_report=_validation_report(sample_size=25, classes={"correct"}),
        minimum_sample_size=20,
    )
    assert report.status == "insufficient_class_coverage"
    assert "critical_failure" in report.rationale


def test_eligibility_never_calibrated_when_severe_disagreements_present():
    severe = [
        SevereDisagreement(
            case_id="c1",
            human_outcome="critical_failure",
            evaluator_outcome="correct",
            severity="critical",
            explanation="missed a hallucination",
        )
    ]
    report = evaluator_reliability_eligibility(
        agreement_report=_agreement(),
        evaluator_validation_report=_validation_report(
            sample_size=25,
            classes={"correct", "critical_failure", "incorrect_refusal"},
            severe=severe,
        ),
        minimum_sample_size=20,
    )
    assert report.status == "provisional"
    assert report.status != "calibrated"


def test_eligibility_clean_result_is_provisional_never_calibrated_outright():
    report = evaluator_reliability_eligibility(
        agreement_report=_agreement(),
        evaluator_validation_report=_validation_report(
            sample_size=25, classes={"correct", "critical_failure", "incorrect_refusal"}
        ),
        minimum_sample_size=20,
    )
    assert report.status == "provisional"
    assert report.status != "calibrated"
