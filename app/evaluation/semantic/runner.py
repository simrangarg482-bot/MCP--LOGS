"""`SemanticBenchmarkRunner` -- orchestrates the Tier 3 benchmark
categories into one `SemanticBenchmarkReport`.

Reuses, never re-implements:
  - `agents.answer.node.generate_answer_with_outcome` (Priority 10) -- the
    same sufficiency -> generate -> grounding sequence, and the same
    authoritative outcome, the production Answer Agent node uses. Given
    either case-supplied evidence text (synthetic cases) or real retrieved
    chunks (repository-derived cases).
  - `agents.investigation.hypothesis.generate_hypotheses` /
    `agents.investigation.critique.review_investigation` -- via
    `investigation_ab.py`.
  - `answer_quality.judge_answer_quality` -- the structured rubric
    evaluator.
  - `agents.telemetry.summarize_usage`/`get_estimated_cost_usd` -- cost
    accounting, the same functions `agents.service._run_graph_and_record`
    already uses for every production execution.
  - `agents.retry.call_with_retry` -- retried around every evaluator call,
    the same convention every other JSON-prompt LLM call in this codebase
    follows.

FAILS LOUDLY, NEVER SILENTLY DOWNGRADES (section 6)
    The credential check itself lives in the caller
    (`scripts/run_semantic_evaluation.py`'s `_require_live_model`, raising
    its own `CredentialsUnavailableError` before this runner is ever
    constructed) -- there is no fallback path anywhere in this module that
    quietly substitutes a deterministic/canned result and reports it as if
    it were live. A credential failure becomes
    `verdict="benchmark_execution_failed"`, distinct from every quality
    verdict `_decide_verdict` below can produce.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from app.agents.answer.node import generate_answer_with_outcome
from app.agents.retry import call_with_retry
from app.agents.telemetry import get_estimated_cost_usd, summarize_usage
from app.evaluation.semantic.answer_quality import judge_answer_quality
from app.evaluation.semantic.fixtures import DATASET_VERSION
from app.evaluation.semantic.investigation_ab import run_investigation_ab_case
from app.evaluation.semantic.outcome import classify_outcome_correctness
from app.evaluation.semantic.schemas import (
    AnswerQualityCase,
    AnswerQualityResult,
    CalibrationReport,
    ExecutionMetadata,
    InvestigationABCase,
    InvestigationABResult,
    SemanticBenchmarkReport,
)
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger

logger = get_logger(__name__)


def _chunk_from_text(text: str, index: int) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="documentation",
        content=text,
        score=1.0 - (index * 0.01),  # stable, deterministic descending order
        source_offset_start=0,
        source_offset_end=len(text),
        title=f"benchmark-evidence-{index}",
    )


async def run_answer_quality_case(
    llm: BaseChatModel, case: AnswerQualityCase
) -> AnswerQualityResult:
    """Generate an answer and judge it with the mode-routed rubric
    evaluator (`answer_quality.judge_answer_quality`), then deterministically
    classify outcome-correctness (`outcome.classify_outcome_correctness`)
    against the case's own declared `expected_answer_mode`.

    Two ways `generated_answer` is obtained, both real, never a canned
    stand-in for a live path:
      - `case.fixed_answer` set: judged directly, with ZERO generation
        calls -- this is what Priority 9's contrast fixtures (section 6)
        use to test the EVALUATOR's discrimination on a specific,
        deliberately-chosen answer text, without needing to coax a
        particular failure mode out of a live model. No authoritative
        production outcome exists for these (no pipeline ran), so
        detection falls through to `judge_answer_quality`'s legacy
        sentinel matching -- deliberately: short-circuiting it here would
        defeat the whole point of a discrimination-testing fixture.
      - `case.fixed_answer` unset: generated live via
        `agents.answer.node.generate_answer_with_outcome` -- Priority 10's
        fix. This is the exact sufficiency -> generate -> grounding
        sequence the production Answer Agent node runs, not a bare
        `generate_answer` call in isolation (Priority 8/9's behavior,
        which is what let `aq-partial-evidence` produce a free-text
        decline no legacy sentinel matched). The resulting
        `AnswerOutcome.mode` is the authoritative TIER 1 signal passed to
        `judge_answer_quality` as `known_mode`, so a decline is correctly
        routed to the refusal rubric regardless of its exact wording.

    `case.evidence_texts` must already be populated in the second path --
    for a `provenance="repository_derived"` case, the caller
    (`scripts/run_semantic_evaluation.py`) is responsible for running real
    retrieval first and constructing a case with `evidence_texts` filled
    in; this function itself never fetches anything.
    """
    handler = UsageMetadataCallbackHandler()
    start = time.monotonic()
    try:
        known_mode = None
        if case.fixed_answer is not None:
            generated_answer = case.fixed_answer
        else:
            if not case.evidence_texts:
                raise ValueError(
                    "case has no evidence_texts -- nothing to generate an answer from"
                )
            chunks = [_chunk_from_text(text, i) for i, text in enumerate(case.evidence_texts)]
            # Retried on a genuine infrastructure failure only -- "no_answer"
            # is now a returned value, not a raised exception (unlike
            # production's own retry-on-decline node() path; see
            # `agents.answer.node`'s module docstring on why that
            # difference is intentional and safe here: a benchmark case
            # legitimately declining is a measured outcome, not a failure
            # to retry away).
            outcome = await call_with_retry(
                "semantic_benchmark.answer_generation",
                lambda: generate_answer_with_outcome(
                    llm.with_config(callbacks=[handler]), case.question, chunks
                ),
                retry_count={},
            )
            generated_answer = outcome.text
            known_mode = "no_answer" if outcome.mode == "no_answer" else None

        judgement = await call_with_retry(
            "semantic_benchmark.answer_quality",
            lambda: judge_answer_quality(
                llm.with_config(callbacks=[handler]), case, generated_answer, known_mode=known_mode
            ),
            retry_count={},
        )
        usage = summarize_usage(handler)
        return AnswerQualityResult(
            case_id=case.id,
            question=case.question,
            generated_answer=generated_answer,
            expected_answer_mode=case.expected_answer_mode,
            judgement=judgement,
            outcome_correctness=classify_outcome_correctness(
                case.expected_answer_mode, judgement.observed_answer_mode
            ),
            latency_seconds=time.monotonic() - start,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001 - one bad case must not abort the run
        logger.warning("answer_quality_case_failed", case_id=case.id, error=str(exc))
        usage = summarize_usage(handler)
        return AnswerQualityResult(
            case_id=case.id,
            question=case.question,
            generated_answer="",
            expected_answer_mode=case.expected_answer_mode,
            judgement=None,
            error=str(exc),
            latency_seconds=time.monotonic() - start,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        )


class SemanticBenchmarkRunner:
    def __init__(
        self, llm: BaseChatModel, *, model_provider: str | None, model_name: str | None
    ) -> None:
        self._llm = llm
        self._model_provider = model_provider
        self._model_name = model_name

    async def run_answer_quality(self, cases: list[AnswerQualityCase]) -> list[AnswerQualityResult]:
        return [await run_answer_quality_case(self._llm, case) for case in cases]

    async def run_investigation_ab(
        self, cases: list[InvestigationABCase]
    ) -> list[InvestigationABResult]:
        return [await run_investigation_ab_case(self._llm, case) for case in cases]

    def build_report(
        self,
        *,
        answer_quality_results: list[AnswerQualityResult],
        investigation_ab_results: list[InvestigationABResult],
        calibration: list[CalibrationReport],
        git_commit: str | None,
    ) -> SemanticBenchmarkReport:
        total_latency = sum(r.latency_seconds for r in answer_quality_results) + sum(
            r.baseline.latency_seconds + r.reflected.latency_seconds
            for r in investigation_ab_results
        )
        total_prompt = sum(r.prompt_tokens for r in answer_quality_results) + sum(
            r.baseline.prompt_tokens + r.reflected.prompt_tokens for r in investigation_ab_results
        )
        total_completion = sum(r.completion_tokens for r in answer_quality_results) + sum(
            r.baseline.completion_tokens + r.reflected.completion_tokens
            for r in investigation_ab_results
        )
        estimated_cost = get_estimated_cost_usd(self._model_name, total_prompt, total_completion)

        verdict, reason = _decide_verdict(
            answer_quality_results, investigation_ab_results, calibration
        )

        return SemanticBenchmarkReport(
            execution=ExecutionMetadata(
                dataset_version=DATASET_VERSION,
                model_provider=self._model_provider,
                model_name=self._model_name,
                generated_at=datetime.now(UTC),
                git_commit=git_commit,
            ),
            execution_succeeded=True,
            answer_quality_results=answer_quality_results,
            investigation_ab_results=investigation_ab_results,
            calibration=calibration,
            total_latency_seconds=total_latency,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            estimated_cost_usd=estimated_cost,
            verdict=verdict,
            verdict_reason=reason,
        )


def _decide_verdict(
    answer_quality_results: list[AnswerQualityResult],
    investigation_ab_results: list[InvestigationABResult],
    calibration: list[CalibrationReport],
) -> tuple[str, str]:
    """Deliberately conservative: a clean execution does not by itself mean
    high quality (this priority's explicit requirement) -- `verdict` is
    only ever `"quality_target_met"` when calibration data actually
    supports that claim; otherwise the honest default is
    `"baseline_established"` (measurements were taken) or
    `"insufficient_data"` (not enough to conclude anything).
    """
    aq_errors = sum(1 for r in answer_quality_results if r.error is not None)
    iab_errors = sum(1 for r in investigation_ab_results if r.error is not None)
    total_cases = len(answer_quality_results) + len(investigation_ab_results)

    if total_cases == 0:
        return "insufficient_data", "no benchmark cases were run"

    error_rate = (aq_errors + iab_errors) / total_cases
    if error_rate > 0.5:
        return (
            "insufficient_data",
            f"more than half of cases ({aq_errors + iab_errors}/{total_cases}) errored -- "
            "results are not representative",
        )

    damaged = sum(1 for r in investigation_ab_results if r.outcome == "critique_damaged")
    if damaged > 0:
        return (
            "regression_detected",
            f"{damaged} investigation A/B case(s) show critique damaging an otherwise "
            "structurally-clean baseline -- see investigation_ab_results for detail",
        )

    critical_failures = sum(
        1 for r in answer_quality_results if r.outcome_correctness == "critical_failure"
    )
    if critical_failures > 0:
        return (
            "regression_detected",
            f"{critical_failures} answer-quality case(s) produced a substantive or qualified "
            "answer where the case declared the evidence had no real bearing on the question "
            "at all (expected_answer_mode='no_answer') -- a hallucination, the single failure "
            "mode this benchmark exists to catch. See answer_quality_results for detail.",
        )

    calibrated = [c for c in calibration if c.status == "calibrated"]
    if calibration and len(calibrated) == len(calibration):
        return "quality_target_met", "every examined threshold is calibrated on this run's data"

    return (
        "baseline_established",
        f"{total_cases} case(s) measured with no errors above the error-rate floor and no "
        "detected regressions; not enough repeated runs/sample size to claim full calibration "
        "-- see the calibration section for per-threshold status",
    )
