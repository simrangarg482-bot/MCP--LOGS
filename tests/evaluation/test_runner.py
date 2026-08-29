"""Integration tests for `EvaluationRunner` against the shipped Mode 1
fixture datasets -- this is the test that actually exercises the full
    Dataset -> Runner -> adapters -> metrics/assertions -> EvaluationResult
pipeline end to end, deterministically, with no external dependency.

The exact pass/fail counts asserted below are a deliberate regression lock:
`app.evaluation.fixtures`' datasets were engineered so exactly the
`expected_outcome="fail"` cases fail and every other case passes (see each
dataset's own `.meta.json` description). If this test ever needs its
expected counts changed, that should mean a fixture case was deliberately
added/changed -- not that the runner's pass/fail logic drifted silently.

These assert on raw pass/fail *and* on `matched_expectation`. The two are
different claims and both matter: raw counts catch the runner's evaluation
logic drifting, while `is_clean`/`regressions` catch the expectation model
itself being mis-wired (e.g. a negative control that stops being recognized
as one). `tests/evaluation/test_ci_gate.py` covers the process-exit-code
layer built on top of these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.datasets.loader import load_dataset
from app.evaluation.fixtures.canned_generations import (
    CANNED_ANSWERS,
    CANNED_CRITIQUES,
    CANNED_INVESTIGATIONS,
)
from app.evaluation.fixtures.corpus import CORPUS
from app.evaluation.runner import EvaluationRunner, build_deterministic_runner
from app.evaluation.schemas import EvaluationCase

_FIXTURES = Path(__file__).resolve().parents[2] / "app" / "evaluation" / "fixtures"


@pytest.fixture()
def runner() -> EvaluationRunner:
    return build_deterministic_runner(
        CORPUS, CANNED_ANSWERS, CANNED_INVESTIGATIONS, canned_critiques=CANNED_CRITIQUES
    )


@pytest.mark.asyncio
async def test_retrieval_dataset_all_cases_pass(runner):
    dataset = load_dataset(_FIXTURES / "retrieval_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    assert report.total == 12
    assert report.failed_count == 0


@pytest.mark.asyncio
async def test_grounding_dataset_only_the_traceable_case_passes(runner):
    dataset = load_dataset(_FIXTURES / "grounding_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    passing_ids = {r.case_id for r in report.results if r.passed}
    assert passing_ids == {"grounding-traceable-001"}
    # Each engineered failure lands in the stage this package's design
    # intends: the two negative controls are retrieval-stage, the three
    # citation defects are generation-stage.
    failures_by_id = {r.case_id: r for r in report.results if not r.passed}
    assert failures_by_id["grounding-untraceable-002"].failure.stage == "retrieval"
    assert failures_by_id["grounding-forbidden-003"].failure.stage == "retrieval"
    assert failures_by_id["grounding-unsupported-citation-004"].failure.stage == "generation"
    assert failures_by_id["grounding-unresolved-citation-005"].failure.stage == "generation"
    assert failures_by_id["grounding-count-006"].failure.stage == "generation"


@pytest.mark.asyncio
async def test_answer_dataset_only_exact_match_case_fails(runner):
    dataset = load_dataset(_FIXTURES / "answer_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    assert report.failed_count == 1
    failure = report.failures[0]
    assert failure.case_id == "answer-exactmatch-005"
    assert failure.failure.stage == "generation"


@pytest.mark.asyncio
async def test_investigation_dataset_only_clear_case_passes(runner):
    dataset = load_dataset(_FIXTURES / "investigation_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    passing_ids = {r.case_id for r in report.results if r.passed}
    assert passing_ids == {
        "investigation-clear-001",
        "investigation-critique-accept-006",
        "investigation-critique-reject-007",
        "investigation-critique-review-failed-008",
        "investigation-critique-revise-then-accept-009",
    }
    failures_by_id = {r.case_id: r for r in report.results if not r.passed}
    assert failures_by_id["investigation-missing-evidence-002"].failure.stage == "retrieval"
    assert failures_by_id["investigation-unmatched-hypothesis-003"].failure.stage == "generation"
    assert failures_by_id["investigation-insufficient-support-004"].failure.stage == "generation"
    hallucination_failure = failures_by_id["investigation-unsupported-hallucination-005"]
    assert hallucination_failure.failure.stage == "generation"
    critique_regression = failures_by_id["investigation-critique-regression-negative-control-010"]
    assert critique_regression.failure.stage == "generation"


@pytest.mark.asyncio
async def test_run_datasets_merges_into_one_report_with_calibration(runner):
    datasets = [
        load_dataset(_FIXTURES / "retrieval_core_v1.jsonl"),
        load_dataset(_FIXTURES / "grounding_core_v1.jsonl"),
        load_dataset(_FIXTURES / "answer_core_v1.jsonl"),
        load_dataset(_FIXTURES / "investigation_core_v1.jsonl"),
    ]
    report = await runner.run_datasets(datasets, combined_name="all")
    assert report.dataset_name == "all"
    assert report.total == 12 + 6 + 5 + 10
    assert report.passed_count == 12 + 1 + 4 + 5
    assert report.calibration is not None
    assert report.calibration.sample_count > 0
    assert report.mode == "deterministic"
    # The invariant that actually matters: every case behaved as its dataset
    # predicted, so the suite is CLEAN despite 11 deliberate failures.
    assert report.is_clean
    assert report.regressions == []
    assert report.expected_failure_count == 11


@pytest.mark.asyncio
async def test_runner_threads_case_expectations_onto_every_result(runner):
    """The runner must copy `expected_outcome`/`expected_failure_stage` from
    each case onto its result -- without this the JSON report can't be
    judged without re-reading the dataset, and `matched_expectation` would
    silently treat every negative control as an unexpected failure."""
    dataset = load_dataset(_FIXTURES / "grounding_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    by_id = {r.case_id: r for r in report.results}

    assert by_id["grounding-traceable-001"].expected_outcome == "pass"
    assert by_id["grounding-untraceable-002"].expected_outcome == "fail"
    assert by_id["grounding-untraceable-002"].expected_failure_stage == "retrieval"
    assert by_id["grounding-count-006"].expected_failure_stage == "generation"
    assert all(r.matched_expectation for r in report.results)


@pytest.mark.asyncio
async def test_answer_category_requires_an_answer_adapter():
    from app.evaluation.adapters.retrieval import FixtureRetrievalAdapter

    bare_runner = EvaluationRunner(
        mode="deterministic", retrieval_adapter=FixtureRetrievalAdapter(CORPUS)
    )
    case = EvaluationCase(id="x", category="answer", query="anything")
    with pytest.raises(RuntimeError, match="AnswerAdapter"):
        await bare_runner.run_case(case)


@pytest.mark.asyncio
async def test_investigation_category_requires_an_investigation_adapter():
    from app.evaluation.adapters.retrieval import FixtureRetrievalAdapter

    bare_runner = EvaluationRunner(
        mode="deterministic", retrieval_adapter=FixtureRetrievalAdapter(CORPUS)
    )
    case = EvaluationCase(id="x", category="investigation", query="anything")
    with pytest.raises(RuntimeError, match="InvestigationAdapter"):
        await bare_runner.run_case(case)


@pytest.mark.asyncio
async def test_aggregate_metrics_present_after_a_run(runner):
    dataset = load_dataset(_FIXTURES / "retrieval_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    assert "mrr" in report.aggregate_metrics
    assert "recall_at_5" in report.aggregate_metrics
    assert "relevant_document_coverage" in report.aggregate_metrics


@pytest.mark.asyncio
async def test_git_commit_recorded_when_available_or_none_gracefully(runner):
    dataset = load_dataset(_FIXTURES / "retrieval_core_v1.jsonl")
    report = await runner.run_dataset(dataset)
    # Either a short hash string or None -- must not raise either way.
    assert report.git_commit is None or isinstance(report.git_commit, str)
