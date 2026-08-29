"""Tests for `app.evaluation.semantic.runner` -- section 15's "aggregation
handles partial failures intentionally," "cost/latency aggregation
correct," and infra-vs-quality-failure distinction requirements, plus
Priority 9's `fixed_answer` contrast-case path and `critical_failure`
verdict wiring.
"""

from __future__ import annotations

import json

import pytest

from app.evaluation.semantic.runner import (
    SemanticBenchmarkRunner,
    _decide_verdict,
    run_answer_quality_case,
)
from app.evaluation.semantic.schemas import (
    AnswerJudgement,
    AnswerQualityCase,
    AnswerQualityDimension,
    AnswerQualityResult,
    CalibrationReport,
    InvestigationABResult,
    InvestigationRunMetrics,
    SubstantiveAnswerJudgement,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _QueuedLLM:
    def __init__(self, *replies: str) -> None:
        self._queue = list(replies)
        self.calls: list[object] = []

    def with_config(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._queue:
            raise AssertionError("LLM called more times than responses were queued")
        return _Response(self._queue.pop(0))


def _case(**overrides) -> AnswerQualityCase:
    payload = {
        "id": "aq-1",
        "provenance": "synthetic_controlled",
        "question": "Why did the service go down?",
        "evidence_texts": ["Deployment 42 reduced the connection pool size."],
    }
    payload.update(overrides)
    return AnswerQualityCase(**payload)


def _valid_substantive_json(**overrides) -> str:
    payload = {
        "observed_mode": "substantive_answer",
        "correctness": {"score": 0.9, "reason": "r"},
        "relevance": {"score": 0.8, "reason": "r"},
        "usefulness": {"score": 0.7, "reason": "r"},
        "faithfulness": {"score": 1.0, "reason": "r"},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _sufficient() -> str:
    """Matches `agents.answer.sufficiency.assess_sufficiency`'s expected
    response shape -- `generate_answer_with_outcome` (Priority 10) now
    calls this FIRST, before generation, for every live-generation case."""
    return "Step 1: the fact.\nStep 2: yes.\nStep 3: no conflict.\nVERDICT: SUFFICIENT"


def _valid_refusal_json() -> str:
    return json.dumps(
        {
            "abstention_correctness": {"score": 0.9, "reason": "r"},
            "unsupported_claim_avoidance": {"score": 1.0, "reason": "r"},
            "explanation_quality": {"score": 0.8, "reason": "r"},
            "appropriate_next_step": {"score": 0.5, "reason": "r"},
        }
    )


async def _always_grounded_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0] for _ in texts]


@pytest.mark.asyncio
async def test_run_answer_quality_case_success_path_has_no_error(monkeypatch):
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)
    llm = _QueuedLLM(
        _sufficient(), "the pool size reduction caused it [1].", _valid_substantive_json()
    )
    result = await run_answer_quality_case(llm, _case())

    assert result.error is None
    assert result.judgement is not None
    assert result.generated_answer.startswith("the pool size reduction caused it")


@pytest.mark.asyncio
async def test_run_answer_quality_case_with_no_evidence_texts_is_a_case_error_not_a_crash():
    llm = _QueuedLLM()  # no calls expected -- fails before ever reaching the model
    result = await run_answer_quality_case(llm, _case(evidence_texts=[]))

    assert result.error is not None
    assert result.judgement is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_run_answer_quality_case_malformed_evaluator_output_is_a_case_error_not_a_crash(
    monkeypatch,
):
    """Distinguishes 'benchmark execution failure' from 'quality failure':
    a malformed evaluator response degrades this ONE case to `.error`, it
    does not raise out of `run_answer_quality_case` and abort the run."""
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)
    llm = _QueuedLLM(
        _sufficient(),
        "the pool size reduction caused it [1].",  # generate_answer succeeds
        "not json",
        "still not json",
        "definitely not json",  # exhausts call_with_retry's attempts
    )
    result = await run_answer_quality_case(llm, _case())

    assert result.error is not None
    assert result.judgement is None
    assert result.generated_answer == ""


def _insufficient() -> str:
    return "Step 1: the fact.\nStep 2: no bearing at all.\nVERDICT: INSUFFICIENT"


def _partial() -> str:
    return "Step 1: the fact.\nStep 2: on-topic only, no specific value.\nVERDICT: PARTIAL"


@pytest.mark.asyncio
async def test_live_generation_end_to_end_reproduces_the_aq_partial_evidence_fix():
    """End-to-end regression test for the exact bug Priority 9's live run
    found: a live-generated case whose evidence doesn't support a specific
    answer must be correctly classified as `no_answer` via the production
    pipeline's own sufficiency decision -- NOT via string-matching whatever
    text `generate_answer` might have produced (in fact, with this fix,
    `generate_answer` is never even called: `assess_sufficiency` short-
    circuits the whole sequence first, exactly like `aq-partial-evidence`'s
    real evidence should have)."""
    llm = _QueuedLLM(_insufficient(), _valid_refusal_json())  # only 2 calls: sufficiency + judge
    case = _case(
        question="What caused the checkout service outage and how was it fixed?",
        evidence_texts=[
            "Checkout service latency spiked at 09:14 UTC; root cause was never "
            "conclusively identified in the available postmortem notes."
        ],
        expected_answer_mode="no_answer",
    )

    result = await run_answer_quality_case(llm, case)

    assert result.error is None
    assert result.judgement.observed_answer_mode == "no_answer"
    assert result.judgement.refusal is not None
    assert result.outcome_correctness == "correct"
    assert len(llm.calls) == 2  # sufficiency + judge -- generation never ran


@pytest.mark.asyncio
async def test_live_generation_partial_evidence_also_short_circuits_to_no_answer():
    llm = _QueuedLLM(_partial(), _valid_refusal_json())
    case = _case(expected_answer_mode="qualified_answer")

    result = await run_answer_quality_case(llm, case)

    assert result.judgement.observed_answer_mode == "no_answer"
    assert result.outcome_correctness == "correct"  # a cautious decline on partial evidence
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_live_generation_calls_exactly_one_judge_call(monkeypatch):
    """Priority 9's own preserved contract: exactly one LLM call for
    JUDGING per case, regardless of how many generation-pipeline calls
    (sufficiency, generation, grounding-escalation) preceded it."""
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)
    llm = _QueuedLLM(
        _sufficient(), "a grounded answer [1].", _valid_substantive_json()
    )
    await run_answer_quality_case(llm, _case())
    # 3 total generation-pipeline calls (sufficiency + generation) + 1 judge
    # call = 3 queued replies consumed exactly, with only the LAST being the
    # judge call -- if judging ever made a second call, this would raise
    # "LLM called more times than responses were queued" instead of passing.
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_fixed_answer_skips_generation_entirely():
    """The contrast-case mechanism (Priority 9 section 6): a case with
    `fixed_answer` set is judged directly, spending zero generation calls
    -- only the one judge call is queued/expected."""
    from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE

    llm = _QueuedLLM(_valid_refusal_json())
    case = _case(fixed_answer=_INSUFFICIENT_GROUNDING_MESSAGE)

    result = await run_answer_quality_case(llm, case)

    assert result.generated_answer == _INSUFFICIENT_GROUNDING_MESSAGE
    assert len(llm.calls) == 1  # only the judge call -- no generate_answer call
    assert result.error is None


@pytest.mark.asyncio
async def test_outcome_correctness_is_computed_from_expected_and_observed_mode():
    from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE

    llm = _QueuedLLM(_valid_refusal_json())
    case = _case(
        fixed_answer=_INSUFFICIENT_GROUNDING_MESSAGE,
        expected_answer_mode="answer",  # evidence was sufficient -- this is a lazy refusal
    )
    result = await run_answer_quality_case(llm, case)
    assert result.outcome_correctness == "incorrect_refusal"


@pytest.mark.asyncio
async def test_unlabeled_case_has_no_outcome_correctness():
    llm = _QueuedLLM(_valid_substantive_json())
    case = _case(fixed_answer="a substantive answer")  # expected_answer_mode defaults to unlabeled
    result = await run_answer_quality_case(llm, case)
    assert result.outcome_correctness is None


def _aq_result(
    *,
    error: str | None,
    mean: float = 0.8,
    expected_answer_mode: str = "answer",
    outcome_correctness: str | None = "correct",
) -> AnswerQualityResult:
    judgement = (
        None
        if error
        else AnswerJudgement(
            observed_answer_mode="substantive_answer",
            substantive=SubstantiveAnswerJudgement(
                observed_mode="substantive_answer",
                correctness=AnswerQualityDimension(score=mean, reason="r"),
                relevance=AnswerQualityDimension(score=mean, reason="r"),
                usefulness=AnswerQualityDimension(score=mean, reason="r"),
                faithfulness=AnswerQualityDimension(score=mean, reason="r"),
            ),
        )
    )
    return AnswerQualityResult(
        case_id="c",
        question="q",
        generated_answer="a" if not error else "",
        expected_answer_mode=expected_answer_mode,
        judgement=judgement,
        outcome_correctness=None if error else outcome_correctness,
        error=error,
        latency_seconds=1.0,
        prompt_tokens=10,
        completion_tokens=5,
    )


def _iab_result(outcome: str, *, error: str | None = None) -> InvestigationABResult:
    metrics = InvestigationRunMetrics(
        hypothesis_count=1,
        mean_confidence=0.5,
        cited_hypothesis_count=1,
        latency_seconds=2.0,
        prompt_tokens=20,
        completion_tokens=10,
    )
    return InvestigationABResult(
        case_id="c", baseline=metrics, reflected=metrics, outcome=outcome, reason="r", error=error
    )


def _calibration(status: str) -> CalibrationReport:
    return CalibrationReport(
        setting_name="x",
        current_value=0.5,
        description="d",
        sample_size=20,
        minimum_sample_size=20,
        status=status,
        rationale="r",
    )


def test_decide_verdict_with_zero_cases_is_insufficient_data():
    verdict, _ = _decide_verdict([], [], [])
    assert verdict == "insufficient_data"


def test_decide_verdict_with_majority_errors_is_insufficient_data():
    results = [_aq_result(error="boom"), _aq_result(error="boom"), _aq_result(error=None)]
    verdict, _ = _decide_verdict(results, [], [])
    assert verdict == "insufficient_data"


def test_decide_verdict_with_critique_damaged_is_regression_detected():
    verdict, reason = _decide_verdict(
        [_aq_result(error=None)], [_iab_result("critique_damaged")], []
    )
    assert verdict == "regression_detected"
    assert "critique" in reason.lower()


def test_decide_verdict_with_critical_failure_is_regression_detected():
    """A hallucinated substantive answer where the case declared the
    evidence had no bearing at all must fail the build, same tier as a
    critique regression -- this is the exact failure mode Priority 9
    exists to catch."""
    verdict, reason = _decide_verdict(
        [_aq_result(error=None, outcome_correctness="critical_failure")], [], []
    )
    assert verdict == "regression_detected"
    assert "hallucination" in reason.lower() or "critical" in reason.lower()


def test_decide_verdict_all_calibrated_is_quality_target_met():
    verdict, _ = _decide_verdict(
        [_aq_result(error=None)],
        [_iab_result("critique_no_measurable_change")],
        [_calibration("calibrated"), _calibration("calibrated")],
    )
    assert verdict == "quality_target_met"


def test_decide_verdict_mixed_calibration_status_is_baseline_established():
    verdict, _ = _decide_verdict(
        [_aq_result(error=None)],
        [_iab_result("critique_no_measurable_change")],
        [_calibration("calibrated"), _calibration("provisional")],
    )
    assert verdict == "baseline_established"


def test_build_report_sums_latency_tokens_and_cost_across_both_categories():
    runner = SemanticBenchmarkRunner(
        _QueuedLLM(), model_provider="openai", model_name="gpt-4o-mini"
    )
    aq_results = [_aq_result(error=None)]  # latency=1.0, tokens=10+5=15
    # latency=2.0+2.0=4.0, tokens=(20+10)*2=60
    iab_results = [_iab_result("critique_no_measurable_change")]

    report = runner.build_report(
        answer_quality_results=aq_results,
        investigation_ab_results=iab_results,
        calibration=[],
        git_commit="abc123",
    )

    assert report.total_latency_seconds == pytest.approx(1.0 + 4.0)
    assert report.total_prompt_tokens == 10 + 40  # 10 (aq) + 20*2 (baseline+reflected)
    assert report.total_completion_tokens == 5 + 20  # 5 (aq) + 10*2 (baseline+reflected)
    assert report.execution.git_commit == "abc123"
    assert report.execution_succeeded is True


def test_build_report_verdict_reflects_a_regression_end_to_end():
    runner = SemanticBenchmarkRunner(
        _QueuedLLM(), model_provider="openai", model_name="gpt-4o-mini"
    )
    report = runner.build_report(
        answer_quality_results=[_aq_result(error=None)],
        investigation_ab_results=[_iab_result("critique_damaged")],
        calibration=[],
        git_commit=None,
    )
    assert report.verdict == "regression_detected"
