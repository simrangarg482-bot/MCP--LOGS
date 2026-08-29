"""Tests for the CI gate itself -- `scripts/run_evaluation.py`'s real
process exit code, plus the shipped fixture suite's own clean verdict.

These invoke the script as a genuine subprocess (`sys.executable
scripts/run_evaluation.py ...`), not by importing and calling `main()`:
the thing CI depends on is the *process exit status*, and only actually
running the process proves that. A `main()` return value that never reaches
`SystemExit` correctly would pass an import-level test and still break CI.

Every case below writes its report into `tmp_path`, so no test here ever
overwrites the repo's own `scripts/run_evaluation_report.json`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "run_evaluation.py"
_FIXTURES = _REPO_ROOT / "app" / "evaluation" / "fixtures"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _write_dataset(tmp_path: Path, name: str, cases: list[dict]) -> Path:
    dataset_path = tmp_path / f"{name}.jsonl"
    dataset_path.write_text(
        "\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8"
    )
    (tmp_path / f"{name}.meta.json").write_text(
        json.dumps({"dataset_name": name, "version": "test", "description": "test dataset"}),
        encoding="utf-8",
    )
    return dataset_path


# --- the exact command CI runs ---------------------------------------------


def test_shipped_fixture_suite_exits_zero(tmp_path):
    """The real CI invocation. Exits 0 despite 10 deliberately-failing
    negative controls, because all 28 cases behave exactly as predicted."""
    result = _run_cli("--report-path", str(tmp_path / "report.json"))
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "VERDICT: CLEAN" in result.stdout
    assert "EVALUATION CLEAN" in result.stdout


def test_shipped_suite_reports_the_expected_case_and_control_counts(tmp_path):
    """Locks in the shipped fixture suite's shape, so silently deleting a
    negative control (the tempting way to "fix" a red build) is itself a
    test failure."""
    result = _run_cli("--report-path", str(tmp_path / "report.json"))
    assert result.returncode == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    results = payload["results"]
    # 28 as of Priority 2 (retrieval 12 / grounding 6 / answer 5 /
    # investigation 5), + 10 in Priority 4's memory_core_v1 = 38,
    # + 6 in Priority 5's graph_core_v1 = 44, + 7 in Priority 6's
    # proactive_core_v1 = 51, + 5 in Priority 7's investigation_core_v1
    # critique additions = 56.
    # Changing these numbers should always mean a fixture case was
    # deliberately added or removed -- never that behavior drifted.
    assert len(results) == 56
    expected_fail = [r for r in results if r["expected_outcome"] == "fail"]
    expected_pass = [r for r in results if r["expected_outcome"] == "pass"]
    # 10 original negative controls + 1 memory-leak control + 1 graph
    # control + 1 proactive control + 1 critique control.
    assert len(expected_fail) == 14
    assert len(expected_pass) == 42
    # Every negative control must actually be failing, and every pinned
    # stage must match -- i.e. the controls are really doing their job, not
    # merely labeled as if they were.
    assert all(r["passed"] is False for r in expected_fail)
    for r in expected_fail:
        if r["expected_failure_stage"] is not None:
            assert r["failure"]["stage"] == r["expected_failure_stage"], r["case_id"]
    assert all(r["passed"] is True for r in expected_pass)


def test_json_report_is_written_and_contains_required_provenance(tmp_path):
    report_path = tmp_path / "report.json"
    result = _run_cli("--report-path", str(report_path))
    assert result.returncode == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    for field in (
        "dataset_name",
        "dataset_version",
        "mode",
        "generated_at",
        "results",
        "aggregate_metrics",
        "calibration",
    ):
        assert field in payload, f"missing report field: {field}"
    assert payload["mode"] == "deterministic"


# --- each regression mode must break the build ----------------------------


def test_unexpected_failure_exits_non_zero(tmp_path):
    """An expected-pass case that fails: the classic regression."""
    dataset = _write_dataset(
        tmp_path,
        "sim_unexpected_failure",
        [
            {
                # This canned answer is a paraphrase, so exact_match really
                # does fail -- but the case claims it should pass.
                "id": "answer-exactmatch-005",
                "category": "answer",
                "query": "What is the exact root cause in one sentence?",
                "expected_outcome": "pass",
                "expected": {
                    "answer_assertions": [
                        {
                            "type": "exact_match",
                            "value": "The root cause was database connection pool exhaustion.",
                        }
                    ]
                },
            }
        ],
    )
    result = _run_cli("--dataset", str(dataset), "--report-path", str(tmp_path / "r.json"))
    assert result.returncode != 0
    assert "UNEXPECTED FAILURE" in result.stdout
    assert "EVALUATION REGRESSED" in result.stdout


def test_unexpected_pass_of_a_negative_control_exits_non_zero(tmp_path):
    """A control that stops detecting its defect. Raw pass count goes UP,
    so only expectation-matching catches this."""
    dataset = _write_dataset(
        tmp_path,
        "sim_unexpected_pass",
        [
            {
                "id": "answer-clear-001",
                "category": "answer",
                "query": "Why did the authentication service fail after deployment 456?",
                "expected_outcome": "fail",
                "expected_failure_stage": "generation",
                "expected": {
                    "answer_assertions": [
                        {"type": "contains_all", "value": ["connection pool", "deployment 456"]}
                    ]
                },
            }
        ],
    )
    result = _run_cli("--dataset", str(dataset), "--report-path", str(tmp_path / "r.json"))
    assert result.returncode != 0
    assert "UNEXPECTED PASS" in result.stdout


def test_wrong_failure_stage_exits_non_zero(tmp_path):
    """Still failing, but no longer testing what it was written to test."""
    dataset = _write_dataset(
        tmp_path,
        "sim_wrong_stage",
        [
            {
                "id": "grounding-count-006",
                "category": "grounding",
                "query": "Why did the authentication service fail?",
                "expected_outcome": "fail",
                # Genuinely fails at "generation" (citation count); pinned
                # here to "retrieval".
                "expected_failure_stage": "retrieval",
                "expected": {
                    "required_concepts": ["connection pool"],
                    "citations": {"minimum": 2, "must_support_answer": True},
                },
            }
        ],
    )
    result = _run_cli("--dataset", str(dataset), "--report-path", str(tmp_path / "r.json"))
    assert result.returncode != 0
    assert "WRONG FAILURE STAGE" in result.stdout


def test_correctly_pinned_negative_control_alone_exits_zero(tmp_path):
    """The mirror of the test above: same case, correct stage -> clean."""
    dataset = _write_dataset(
        tmp_path,
        "sim_correct_stage",
        [
            {
                "id": "grounding-count-006",
                "category": "grounding",
                "query": "Why did the authentication service fail?",
                "expected_outcome": "fail",
                "expected_failure_stage": "generation",
                "expected": {
                    "required_concepts": ["connection pool"],
                    "citations": {"minimum": 2, "must_support_answer": True},
                },
            }
        ],
    )
    result = _run_cli("--dataset", str(dataset), "--report-path", str(tmp_path / "r.json"))
    assert result.returncode == 0, result.stdout
    assert "VERDICT: CLEAN" in result.stdout


# --- the gate must not report success when it never really ran ------------


def test_malformed_dataset_exits_non_zero(tmp_path):
    dataset_path = tmp_path / "malformed.jsonl"
    dataset_path.write_text('{"id": "bad", "category": "retrieval"\n', encoding="utf-8")
    result = _run_cli("--dataset", str(dataset_path), "--report-path", str(tmp_path / "r.json"))
    assert result.returncode != 0
    assert "invalid JSON" in result.stderr or "invalid JSON" in result.stdout


def test_missing_dataset_file_exits_non_zero(tmp_path):
    result = _run_cli(
        "--dataset",
        str(tmp_path / "does_not_exist.jsonl"),
        "--report-path",
        str(tmp_path / "r.json"),
    )
    assert result.returncode != 0


def test_unwritable_report_path_exits_non_zero(tmp_path):
    """"The JSON report cannot be generated" must fail the gate -- a run
    whose evidence was never persisted must not read as a pass."""
    unwritable = tmp_path / "no_such_dir" / "nested" / "report.json"
    result = _run_cli("--report-path", str(unwritable))
    assert result.returncode != 0


@pytest.mark.parametrize(
    "dataset_name",
    [
        "retrieval_core_v1",
        "grounding_core_v1",
        "answer_core_v1",
        "investigation_core_v1",
        "memory_core_v1",
    ],
)
def test_each_shipped_dataset_is_independently_clean(dataset_name, tmp_path):
    """Each dataset must stand on its own, so a regression can be localized
    to one dataset rather than only ever surfacing in the combined run."""
    result = _run_cli(
        "--dataset",
        str(_FIXTURES / f"{dataset_name}.jsonl"),
        "--report-path",
        str(tmp_path / f"{dataset_name}.json"),
    )
    assert result.returncode == 0, f"{dataset_name} regressed:\n{result.stdout}"
