"""Investigation Agent baseline-vs-reflection A/B benchmark -- Tier 3.

Does NOT create a second Investigation Agent. Both runs call the exact
same production functions the real graph node (`agents.investigation.
node.make_investigation_agent_node`) already calls:

    baseline  = agents.investigation.hypothesis.generate_hypotheses(...)
    reflected = agents.investigation.critique.review_investigation(...)
                applied to baseline's OWN output

EQUIVALENT INPUTS, GUARANTEED BY CONSTRUCTION
    Rather than running hypothesis generation twice (once "for baseline",
    once "for reflected") and hoping two independent LLM samples are
    comparable, this harness generates hypotheses ONCE and treats that
    single draft as `baseline`; `reflected` is what critique does to that
    SAME draft. This isolates exactly one variable -- "did critique review
    this" -- from LLM sampling variance, which two independent generations
    could never do. It is also literally what the real production node
    does when `investigation_critique_enabled=True`: generate once, then
    critique -- this benchmark's "reflected" run is not a simulation of
    that path, it is that path, called directly.

OUTCOME CLASSIFICATION IS A STRUCTURAL PROXY, NOT A GROUND-TRUTH JUDGMENT
    `_classify_outcome` compares `critique.validate_structurally`'s
    findings against the baseline draft with what the reflected run
    actually did (accept unchanged / apply a penalty / reject / revise).
    It answers "did critique's bounded, deterministic checks catch
    something real about the baseline, and did its action match that
    finding" -- not "was the reflected hypothesis semantically better,"
    which would need a human or a separate ground-truth judgment this
    package does not have. See `docs/SEMANTIC_BENCHMARK.md`'s
    "Investigation A/B methodology" section for the honest limitation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from app.agents.investigation import critique as critique_module
from app.agents.investigation.hypothesis import generate_hypotheses
from app.agents.telemetry import summarize_usage
from app.evaluation.semantic.schemas import (
    InvestigationABCase,
    InvestigationABOutcome,
    InvestigationABResult,
    InvestigationRunMetrics,
)
from app.shared.config.logging import get_logger
from app.shared.schemas import EvidenceItem, RootCauseHypothesis

logger = get_logger(__name__)


def _build_evidence(case: InvestigationABCase) -> list[EvidenceItem]:
    now = datetime.now(UTC)
    return [
        EvidenceItem(source=source, reference=reference, summary=summary, retrieved_at=now)  # type: ignore[arg-type]
        for reference, source, summary in case.evidence
    ]


def _mean_confidence(hypotheses: list[RootCauseHypothesis]) -> float | None:
    if not hypotheses:
        return None
    return sum(h.confidence for h in hypotheses) / len(hypotheses)


def _metrics(
    hypotheses: list[RootCauseHypothesis],
    *,
    review_status: str | None,
    critique_verdict: str | None,
    revision_count: int,
    latency_seconds: float,
    usage: dict[str, object],
) -> InvestigationRunMetrics:
    return InvestigationRunMetrics(
        hypothesis_count=len(hypotheses),
        mean_confidence=_mean_confidence(hypotheses),
        cited_hypothesis_count=sum(1 for h in hypotheses if h.supporting_evidence_ids),
        review_status=review_status,
        critique_verdict=critique_verdict,
        revision_count=revision_count,
        latency_seconds=latency_seconds,
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


def _classify_outcome(
    baseline_hypotheses: list[RootCauseHypothesis],
    baseline_structural_issues: list[str],
    reflected: critique_module.ReviewOutcome,
) -> tuple[InvestigationABOutcome, str]:
    if reflected.review_status != "reviewed":
        return (
            "critique_unavailable",
            f"critique did not complete (review_status={reflected.review_status!r})",
        )

    baseline_had_issues = bool(baseline_structural_issues)

    if reflected.critique_verdict == "reject":
        if baseline_had_issues or not baseline_hypotheses:
            return (
                "critique_correctly_rejected",
                f"baseline had detectable structural issues {baseline_structural_issues!r} "
                "(or produced no hypotheses); critique rejected",
            )
        return (
            "critique_damaged",
            "critique rejected a baseline with no detected structural issues -- a possible "
            "over-rejection; see docs/SEMANTIC_BENCHMARK.md's methodology limitation",
        )

    # accept (possibly after the one bounded revision)
    baseline_conf = _mean_confidence(baseline_hypotheses)
    reflected_conf = _mean_confidence(reflected.hypotheses)

    unchanged = (
        not baseline_had_issues
        and reflected.revision_count == 0
        and len(reflected.hypotheses) == len(baseline_hypotheses)
        and reflected_conf == baseline_conf
    )
    if unchanged:
        return (
            "critique_no_measurable_change",
            "no structural issues found in the baseline; critique accepted it unchanged",
        )

    if reflected.revision_count == 1:
        return (
            "critique_improved",
            "the one bounded revision changed the hypothesis set in response to critique feedback",
        )

    if baseline_conf is not None and reflected_conf is not None and reflected_conf < baseline_conf:
        return (
            "critique_improved",
            f"confidence adjusted down from {baseline_conf:.2f} to {reflected_conf:.2f} after "
            f"flagging {baseline_structural_issues!r}",
        )

    return "critique_no_measurable_change", "critique accepted without a detectable change"


async def run_investigation_ab_case(
    llm: BaseChatModel, case: InvestigationABCase
) -> InvestigationABResult:
    """Run one A/B case. Never raises -- an exception anywhere in either
    run is captured on `InvestigationABResult.error` so one bad case
    cannot abort the whole benchmark, the same failure-isolation
    convention `core.proactive.service._run_one_detector` and
    `agents.investigation.evidence.gather_evidence` already establish.
    """
    evidence = _build_evidence(case)
    try:
        baseline_handler = UsageMetadataCallbackHandler()
        start = time.monotonic()
        baseline_hypotheses, owner_team, next_steps = await generate_hypotheses(
            llm.with_config(callbacks=[baseline_handler]), case.query, evidence
        )
        baseline_latency = time.monotonic() - start
        baseline_usage = summarize_usage(baseline_handler)

        baseline_issues = critique_module.validate_structurally(
            baseline_hypotheses,
            evidence,
            min_evidence_count=2,
            overconfidence_threshold=0.75,
            min_evidence_per_hypothesis=2,
        )

        reflected_handler = UsageMetadataCallbackHandler()
        start = time.monotonic()
        reflected = await critique_module.review_investigation(
            llm.with_config(callbacks=[reflected_handler]),
            case.query,
            evidence,
            baseline_hypotheses,
            owner_team,
            next_steps,
            {},
        )
        reflected_latency = time.monotonic() - start
        reflected_usage = summarize_usage(reflected_handler)

        baseline_metrics = _metrics(
            baseline_hypotheses,
            review_status="not_reviewed",
            critique_verdict=None,
            revision_count=0,
            latency_seconds=baseline_latency,
            usage=baseline_usage,
        )
        reflected_metrics = _metrics(
            reflected.hypotheses,
            review_status=reflected.review_status,
            critique_verdict=reflected.critique_verdict,
            revision_count=reflected.revision_count,
            latency_seconds=reflected_latency,
            usage=reflected_usage,
        )
        outcome, reason = _classify_outcome(baseline_hypotheses, baseline_issues, reflected)

        return InvestigationABResult(
            case_id=case.id,
            baseline=baseline_metrics,
            reflected=reflected_metrics,
            outcome=outcome,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - one bad case must not abort the whole benchmark
        logger.warning("investigation_ab_case_failed", case_id=case.id, error=str(exc))
        empty = InvestigationRunMetrics(
            hypothesis_count=0, mean_confidence=None, cited_hypothesis_count=0
        )
        return InvestigationABResult(
            case_id=case.id,
            baseline=empty,
            reflected=empty,
            outcome="critique_unavailable",
            reason="benchmark case execution failed",
            error=str(exc),
        )
