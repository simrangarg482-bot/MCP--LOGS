"""Tests for `app.evaluation.reporting`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.evaluation.reporting.console import render_console_report
from app.evaluation.reporting.json_report import render_json_report, write_json_report
from app.evaluation.schemas import EvaluationReport, EvaluationResult, FailureDetail

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _result(case_id: str, passed: bool, stage: str = "none") -> EvaluationResult:
    return EvaluationResult(
        case_id=case_id,
        category="retrieval",
        mode="deterministic",
        passed=passed,
        failure=FailureDetail(stage=stage, reason="ok" if passed else "something failed"),
        timestamp=_NOW,
    )


def _report() -> EvaluationReport:
    return EvaluationReport(
        dataset_name="sample",
        dataset_version="1.0",
        mode="deterministic",
        generated_at=_NOW,
        results=[_result("pass-1", True), _result("fail-1", False, stage="retrieval")],
    )


def test_console_report_includes_required_fields():
    output = render_console_report(_report())
    assert "sample" in output
    assert "1.0" in output
    assert "deterministic" in output
    assert "Total cases: 2" in output
    assert "Passed: 1" in output
    assert "Failed: 1" in output
    assert "fail-1" in output
    assert "retrieval" in output


def test_json_report_round_trips_through_pydantic():
    rendered = render_json_report(_report())
    payload = json.loads(rendered)
    assert payload["dataset_name"] == "sample"
    assert len(payload["results"]) == 2
    restored = EvaluationReport.model_validate(payload)
    assert restored.total == 2
    assert restored.passed_count == 1


def test_write_json_report_creates_readable_file(tmp_path):
    path = tmp_path / "report.json"
    write_json_report(_report(), path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dataset_name"] == "sample"


def test_report_aggregate_properties():
    report = _report()
    assert report.total == 2
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert [r.case_id for r in report.failures] == ["fail-1"]
