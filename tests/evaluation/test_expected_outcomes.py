"""Tests for the expected-outcome / regression model -- the logic the CI
gate is built on (`EvaluationCase.expected_outcome`,
`EvaluationResult.matched_expectation`/`regression_kind`, and
`EvaluationReport`'s regression accounting).

These are deliberately the most heavily-tested part of this package: an
error here doesn't produce a wrong metric, it produces a CI gate that
either cries wolf on every build or -- far worse -- goes green while real
regressions land.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.evaluation.schemas import (
    EvaluationCase,
    EvaluationReport,
    EvaluationResult,
    FailureDetail,
)

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _result(
    case_id: str,
    *,
    passed: bool,
    stage: str = "none",
    expected_outcome: str = "pass",
    expected_failure_stage: str | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        case_id=case_id,
        category="retrieval",
        mode="deterministic",
        passed=passed,
        failure=FailureDetail(stage=stage, reason="ok" if passed else "failed"),
        expected_outcome=expected_outcome,
        expected_failure_stage=expected_failure_stage,
        timestamp=_NOW,
    )


# --- matched_expectation ---------------------------------------------------


def test_expected_pass_that_passes_matches():
    result = _result("c", passed=True, expected_outcome="pass")
    assert result.matched_expectation
    assert result.regression_kind is None


def test_expected_pass_that_fails_is_an_unexpected_failure():
    result = _result("c", passed=False, stage="retrieval", expected_outcome="pass")
    assert not result.matched_expectation
    assert result.regression_kind == "unexpected_failure"


def test_expected_fail_that_fails_at_the_pinned_stage_matches():
    result = _result(
        "c",
        passed=False,
        stage="generation",
        expected_outcome="fail",
        expected_failure_stage="generation",
    )
    assert result.matched_expectation
    assert result.regression_kind is None


def test_expected_fail_that_fails_at_the_wrong_stage_is_a_regression():
    result = _result(
        "c",
        passed=False,
        stage="retrieval",
        expected_outcome="fail",
        expected_failure_stage="generation",
    )
    assert not result.matched_expectation
    assert result.regression_kind == "wrong_failure_stage"


def test_expected_fail_with_no_pinned_stage_matches_any_failure_stage():
    for stage in ("retrieval", "generation"):
        result = _result("c", passed=False, stage=stage, expected_outcome="fail")
        assert result.matched_expectation, f"stage={stage} should satisfy an unpinned expectation"


def test_expected_fail_that_passes_is_an_unexpected_pass():
    """The nastiest regression: a negative control stops detecting anything.
    Raw pass counts go UP, which is exactly why the gate cannot use them."""
    result = _result("c", passed=True, expected_outcome="fail", expected_failure_stage=None)
    assert not result.matched_expectation
    assert result.regression_kind == "unexpected_pass"


def test_expected_fail_that_passes_is_a_regression_even_with_a_pinned_stage():
    result = _result("c", passed=True, expected_outcome="fail", expected_failure_stage="generation")
    assert result.regression_kind == "unexpected_pass"


# --- case-level schema validation -----------------------------------------


def test_case_defaults_to_expected_pass():
    case = EvaluationCase(id="c", category="retrieval", query="q")
    assert case.expected_outcome == "pass"
    assert case.expected_failure_stage is None


def test_pinned_stage_rejected_when_case_is_expected_to_pass():
    with pytest.raises(ValidationError, match="only meaningful when expected_outcome is 'fail'"):
        EvaluationCase(
            id="c", category="retrieval", query="q", expected_failure_stage="retrieval"
        )


def test_pinned_stage_of_none_is_rejected_as_ambiguous():
    with pytest.raises(ValidationError, match="cannot be 'none'"):
        EvaluationCase(
            id="c",
            category="retrieval",
            query="q",
            expected_outcome="fail",
            expected_failure_stage="none",
        )


def test_expected_fail_case_accepts_a_pinned_stage():
    case = EvaluationCase(
        id="c",
        category="retrieval",
        query="q",
        expected_outcome="fail",
        expected_failure_stage="generation",
    )
    assert case.expected_failure_stage == "generation"


# --- report-level accounting ----------------------------------------------


def test_report_is_clean_when_every_case_matches_expectation():
    report = EvaluationReport(
        dataset_name="d",
        dataset_version="1.0",
        mode="deterministic",
        generated_at=_NOW,
        results=[
            _result("pass-1", passed=True),
            _result(
                "control-1",
                passed=False,
                stage="generation",
                expected_outcome="fail",
                expected_failure_stage="generation",
            ),
        ],
    )
    assert report.is_clean
    assert report.regressions == []
    # A clean report still has a non-zero raw failure count -- the whole
    # point of separating the two.
    assert report.failed_count == 1
    assert report.expected_failure_count == 1


def test_report_partitions_the_three_regression_kinds():
    report = EvaluationReport(
        dataset_name="d",
        dataset_version="1.0",
        mode="deterministic",
        generated_at=_NOW,
        results=[
            _result("ok", passed=True),
            _result("regress-fail", passed=False, stage="retrieval", expected_outcome="pass"),
            _result("regress-pass", passed=True, expected_outcome="fail"),
            _result(
                "regress-stage",
                passed=False,
                stage="retrieval",
                expected_outcome="fail",
                expected_failure_stage="generation",
            ),
        ],
    )
    assert not report.is_clean
    assert len(report.regressions) == 3
    assert [r.case_id for r in report.unexpected_failures] == ["regress-fail"]
    assert [r.case_id for r in report.unexpected_passes] == ["regress-pass"]
    assert [r.case_id for r in report.wrong_stage_failures] == ["regress-stage"]


def test_a_suite_of_only_correctly_detected_controls_is_clean():
    """All cases fail; all are supposed to. `failed_count == total` and yet
    the verdict is CLEAN -- the exact scenario a naive `exit 1 if failures`
    gate gets wrong."""
    report = EvaluationReport(
        dataset_name="d",
        dataset_version="1.0",
        mode="deterministic",
        generated_at=_NOW,
        results=[
            _result(f"control-{i}", passed=False, stage="generation", expected_outcome="fail")
            for i in range(5)
        ],
    )
    assert report.failed_count == report.total == 5
    assert report.passed_count == 0
    assert report.is_clean


def test_report_json_round_trip_preserves_expectation_fields():
    """The JSON artifact must be self-contained enough to re-derive the
    verdict without the dataset it came from."""
    report = EvaluationReport(
        dataset_name="d",
        dataset_version="1.0",
        mode="deterministic",
        generated_at=_NOW,
        results=[
            _result(
                "control",
                passed=False,
                stage="generation",
                expected_outcome="fail",
                expected_failure_stage="generation",
            ),
            _result("regress", passed=True, expected_outcome="fail"),
        ],
    )
    restored = EvaluationReport.model_validate_json(report.model_dump_json())
    assert restored.results[0].expected_outcome == "fail"
    assert restored.results[0].expected_failure_stage == "generation"
    assert restored.results[0].matched_expectation
    assert restored.results[1].regression_kind == "unexpected_pass"
    assert not restored.is_clean
