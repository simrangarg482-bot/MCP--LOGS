"""Tier 3 CLI entry point for `app.evaluation.semantic` -- the live,
real-model answer-quality + Investigation A/B benchmark, plus threshold
calibration reporting.

Mirrors `scripts/eval_confidence.py`/`scripts/run_evaluation.py`'s own CLI
conventions (argparse, `--report-path`, printing to stdout as it goes) for
consistency with this project's existing operational scripts.

WHAT THIS SCRIPT DOES NOT DO
    It does not re-implement confidence-threshold routing evaluation or
    answer-grounding-rate measurement -- `scripts/eval_confidence.py`
    already does both, against real ingested `test-org` data, with its own
    CI job (`.github/workflows/e2e-and-eval.yml`'s `ai-evaluation`). This
    script's calibration section REUSES that script's last written report
    (`--eval-confidence-report`) rather than duplicating it. It also does
    not re-implement retrieval/grounding/citation PASS-FAIL validation --
    `tests/rag_validation/run_validation.py` already does that, with its
    own separate LLM judge kept deliberately apart from EKIP's own
    `verify_grounding` (see that directory's README "Why the judge is
    separate"). This script covers what neither of those does: a
    structured, multi-dimension answer-quality rubric, and the
    Investigation Agent baseline-vs-reflection A/B comparison.

RUN
    python scripts/run_semantic_evaluation.py                       # synthetic corpus only
    python scripts/run_semantic_evaluation.py --repository-derived  # + real test-org questions
    python scripts/run_semantic_evaluation.py --limit 2             # quick smoke run
    python scripts/run_semantic_evaluation.py --report-path scripts/semantic_report.json

CREDENTIAL FAILURE (section 6)
    Fails loudly, immediately, with a non-zero exit and a clear message, if
    no live model can be reached -- never silently substitutes a
    deterministic/canned result and reports it as if it were live. See
    `_require_live_model`.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.evaluation.semantic import (  # noqa: E402
    annotation_store,
    evaluator_validation,
    ground_truth,
)
from app.evaluation.semantic.calibration import (  # noqa: E402
    DEFAULT_MINIMUM_SAMPLE_SIZE,
    calibration_from_eval_confidence_report,
    evaluator_reliability_eligibility,
)
from app.evaluation.semantic.fixtures import (  # noqa: E402
    CONTRAST_ANSWER_QUALITY_CASES,
    DATASET_VERSION,
    SYNTHETIC_ANSWER_QUALITY_CASES,
    SYNTHETIC_INVESTIGATION_AB_CASES,
    load_annotatable_answer_quality_cases,
    load_repository_derived_annotatable_cases,
    load_repository_derived_answer_quality_cases,
)
from app.evaluation.semantic.runner import SemanticBenchmarkRunner  # noqa: E402
from app.evaluation.semantic.schemas import (  # noqa: E402
    AnswerQualityCase,
    AnswerQualityResult,
    CalibrationReport,
    HumanGroundTruthCoverage,
    SemanticBenchmarkReport,
)
from app.shared.config.settings import get_settings  # noqa: E402

_DEFAULT_REPORT_PATH = _REPO_ROOT / "scripts" / "run_semantic_evaluation_report.json"
_DEFAULT_EVAL_CONFIDENCE_REPORT = _REPO_ROOT / "scripts" / "eval_confidence_report_after.json"


class CredentialsUnavailableError(RuntimeError):
    pass


def _require_live_model() -> tuple[str, str]:
    """Fail loudly, immediately, if no live model can be reached -- the
    same `is_available()` check `app.evaluation.adapters.llm.RealLLMJudge`
    already performs, reused rather than re-implemented, plus the extra
    honesty of actually raising instead of returning a bool a caller could
    ignore.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        raise CredentialsUnavailableError(f"could not load Settings: {exc}") from exc
    if not (settings.openai_api_key and settings.openai_api_key.strip()):
        raise CredentialsUnavailableError(
            "OPENAI_API_KEY is not configured -- this is a live, real-model benchmark "
            "(Tier 3) and cannot run without one. It will NOT silently fall back to a "
            "deterministic result; use `uv run python scripts/run_evaluation.py` for the "
            "credential-free deterministic (Tier 1) evaluation instead."
        )
    return "openai", settings.agent_llm_model


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _print_answer_quality(report: SemanticBenchmarkReport) -> None:
    from collections import Counter

    print("\nAnswer Quality", flush=True)
    print("--------------", flush=True)
    results = report.answer_quality_results
    ok = [r for r in results if r.judgement is not None]
    errored = [r for r in results if r.error is not None]
    print(f"  cases: {len(results)}  (evaluated: {len(ok)}, errored: {len(errored)})", flush=True)

    observed_counts = Counter(r.judgement.observed_answer_mode for r in ok)
    if observed_counts:
        print("  observed mode:", flush=True)
        for mode, count in sorted(observed_counts.items()):
            print(f"    {mode:<20} {count}", flush=True)

    # Outcome correctness (section 3): only cases with a non-"unlabeled"
    # expected_answer_mode contribute -- unlabeled cases are excluded from
    # this metric entirely, never silently counted as correct.
    scored = [r for r in ok if r.outcome_correctness is not None]
    unlabeled = len(ok) - len(scored)
    if scored:
        print(
            f"  outcome correctness (excludes {unlabeled} unlabeled case(s)):",
            flush=True,
        )
        outcome_counts = Counter(r.outcome_correctness for r in scored)
        for outcome, count in sorted(outcome_counts.items()):
            print(f"    {outcome:<20} {count}", flush=True)
    elif unlabeled:
        print(
            f"  outcome correctness: not computed -- all {unlabeled} case(s) unlabeled", flush=True
        )

    substantive = [r for r in ok if r.judgement.substantive is not None]
    if substantive:

        def _mean(attr: str) -> float:
            return sum(getattr(r.judgement.substantive, attr).score for r in substantive) / len(
                substantive
            )

        print(f"  substantive rubric (n={len(substantive)}):", flush=True)
        print(f"    correctness  : {_mean('correctness'):.3f}", flush=True)
        print(f"    relevance    : {_mean('relevance'):.3f}", flush=True)
        print(f"    usefulness   : {_mean('usefulness'):.3f}", flush=True)
        print(f"    faithfulness : {_mean('faithfulness'):.3f}", flush=True)

    refusals = [r for r in ok if r.judgement.refusal is not None]
    if refusals:

        def _mean_refusal(attr: str) -> float:
            return sum(getattr(r.judgement.refusal, attr).score for r in refusals) / len(refusals)

        print(f"  refusal rubric (n={len(refusals)}):", flush=True)
        print(
            f"    abstention_correctness      : {_mean_refusal('abstention_correctness'):.3f}",
            flush=True,
        )
        print(
            f"    unsupported_claim_avoidance : {_mean_refusal('unsupported_claim_avoidance'):.3f}",
            flush=True,
        )
        print(
            f"    explanation_quality         : {_mean_refusal('explanation_quality'):.3f}",
            flush=True,
        )
        print(
            f"    appropriate_next_step       : {_mean_refusal('appropriate_next_step'):.3f}",
            flush=True,
        )

    for r in errored:
        print(f"  [ERROR] {r.case_id}: {r.error}", flush=True)


def _print_investigation_ab(report: SemanticBenchmarkReport) -> None:
    print("\nInvestigation A/B", flush=True)
    print("-----------------", flush=True)
    results = report.investigation_ab_results
    print(f"  cases: {len(results)}", flush=True)
    from collections import Counter

    counts = Counter(r.outcome for r in results)
    for outcome, count in sorted(counts.items()):
        print(f"  {outcome:<32} {count}", flush=True)
    baseline_latency = sum(r.baseline.latency_seconds for r in results)
    reflected_latency = sum(r.reflected.latency_seconds for r in results)
    baseline_tokens = sum(r.baseline.prompt_tokens + r.baseline.completion_tokens for r in results)
    reflected_tokens = sum(
        r.reflected.prompt_tokens + r.reflected.completion_tokens for r in results
    )
    print(
        f"  latency  -- baseline: {baseline_latency:.2f}s  reflected: {reflected_latency:.2f}s  "
        f"(delta: {reflected_latency - baseline_latency:+.2f}s)",
        flush=True,
    )
    print(
        f"  tokens   -- baseline: {baseline_tokens}  reflected: {reflected_tokens}  "
        f"(delta: {reflected_tokens - baseline_tokens:+d})",
        flush=True,
    )


def _print_calibration(calibration: list[CalibrationReport]) -> None:
    print("\nCalibration Candidates", flush=True)
    print("----------------------", flush=True)
    by_status: dict[str, list[str]] = {}
    for entry in calibration:
        by_status.setdefault(entry.status, []).append(entry.setting_name)
    for status in (
        "calibrated",
        "provisional",
        "insufficient_data",
        "intentionally_fixed_domain_rule",
    ):
        names = by_status.get(status, [])
        print(f"  {status:<32} {', '.join(names) if names else '(none)'}", flush=True)
    for entry in calibration:
        print(
            f"\n  {entry.setting_name} (current={entry.current_value}, n={entry.sample_size})",
            flush=True,
        )
        print(f"    status: {entry.status}", flush=True)
        print(f"    {entry.rationale}", flush=True)


def _print_human_ground_truth(coverage: HumanGroundTruthCoverage | None) -> None:
    print("\nHuman Ground Truth Coverage", flush=True)
    print("---------------------------", flush=True)
    if coverage is None:
        print("  human validation unavailable -- no annotatable cases were run", flush=True)
        return
    print(f"  total annotatable cases     : {coverage.total_cases}", flush=True)
    print(f"  annotated cases             : {coverage.annotated_cases}", flush=True)
    print(f"  double-reviewed cases       : {coverage.double_reviewed_cases}", flush=True)
    print(f"  agreed cases                : {coverage.agreed_cases}", flush=True)
    print(f"  unresolved disagreements    : {coverage.unresolved_disagreements}", flush=True)
    print(
        f"  eligible for validation     : {coverage.eligible_for_evaluator_validation}", flush=True
    )
    print(f"  provenance                  : {coverage.provenance_counts}", flush=True)
    if coverage.annotated_cases == 0:
        print(
            "  human validation unavailable -- 0 human-adjudicated cases; run "
            "scripts/annotate_semantic_cases.py to record annotations",
            flush=True,
        )


def _print_evaluator_validation(
    report: evaluator_validation.EvaluatorValidationReport | None,
) -> None:
    print("\nEvaluator vs Human Agreement", flush=True)
    print("----------------------------", flush=True)
    if report is None:
        print(
            "  evaluator-human agreement: unavailable -- no resolved ground truth to compare",
            flush=True,
        )
        return
    print(
        f"  sample size (n)         : {report.sample_size} (floor: {report.minimum_sample_size})",
        flush=True,
    )
    print(f"  status                  : {report.status}", flush=True)
    if report.answer_mode_agreement_rate is not None:
        print(f"  answer-mode agreement   : {report.answer_mode_agreement_rate:.2f}", flush=True)
    if report.outcome_agreement_rate is not None:
        print(f"  outcome agreement       : {report.outcome_agreement_rate:.2f}", flush=True)
    print(f"  excluded cases          : {report.excluded_case_count}", flush=True)
    if report.severe_disagreements:
        print(f"  SEVERE DISAGREEMENTS ({len(report.severe_disagreements)}):", flush=True)
        for sd in report.severe_disagreements:
            print(
                f"    [{sd.severity.upper()}] {sd.case_id}: human={sd.human_outcome} "
                f"evaluator={sd.evaluator_outcome} -- {sd.explanation}",
                flush=True,
            )
    if report.outcome_confusion_matrix:
        print("  confusion matrix (human -> evaluator : count):", flush=True)
        for cell in report.outcome_confusion_matrix:
            print(f"    {cell.human_label} -> {cell.evaluator_label} : {cell.count}", flush=True)
    for metric in report.outcome_class_metrics:
        p = f"{metric.precision:.2f}" if metric.precision is not None else "n/a"
        r = f"{metric.recall:.2f}" if metric.recall is not None else "n/a"
        f1 = f"{metric.f1:.2f}" if metric.f1 is not None else "n/a"
        print(
            f"    class={metric.label:<20} support={metric.support} precision={p} "
            f"recall={r} f1={f1}",
            flush=True,
        )
    print(f"  {report.rationale}", flush=True)


def _build_human_ground_truth_report(
    answer_quality_cases: list[AnswerQualityCase],
    answer_quality_results: list[AnswerQualityResult],
    *,
    dataset_version: str,
) -> tuple[
    HumanGroundTruthCoverage | None,
    ground_truth.AgreementReport | None,
    evaluator_validation.EvaluatorValidationReport | None,
    CalibrationReport | None,
]:
    """Priority 11: compares this run's `answer_quality_results` against
    whatever human annotations already exist for the human-annotatable
    corpus. Every case here always has `fixed_answer` set (see
    `fixtures.load_annotatable_answer_quality_cases`'s own docstring), so
    the generated answer is identical every run -- an annotation's
    `case_snapshot_hash` will always match unless the case's own question/
    evidence/candidate-answer text changed, which this function still
    checks for defensively rather than assuming.
    """
    annotatable_cases = {c.id: c for c in load_annotatable_answer_quality_cases()}
    run_case_ids = {c.id for c in answer_quality_cases}
    # Only cases this run actually included AND that are annotatable.
    relevant_ids = run_case_ids & annotatable_cases.keys()
    if not relevant_ids:
        return None, None, None, None

    results_by_case = {r.case_id: r for r in answer_quality_results if r.case_id in relevant_ids}
    annotations_by_case: dict = {}
    for annotation in annotation_store.load_annotations(dataset_version):
        if annotation.case_id in relevant_ids:
            annotations_by_case.setdefault(annotation.case_id, []).append(annotation)
    resolutions_by_case = {
        resolution.case_id: resolution
        for resolution in annotation_store.load_resolutions(dataset_version)
        if resolution.case_id in relevant_ids
    }

    provenance_counts: dict[str, int] = {}
    for case_id in annotations_by_case:
        case = annotatable_cases[case_id]
        provenance_counts[case.provenance] = provenance_counts.get(case.provenance, 0) + 1

    ground_truths_by_case = {}
    agreed = 0
    unresolved = 0
    double_reviewed = 0
    for case_id, anns in annotations_by_case.items():
        case = annotatable_cases[case_id]
        gt = ground_truth.resolve_ground_truth(case, anns, resolutions_by_case.get(case_id))
        # Staleness check (section 12): only trust this annotation against
        # THIS run's result if the candidate answer it was recorded against
        # is exactly what this run actually produced.
        result = results_by_case.get(case_id)
        if result is not None:
            current_hash = annotation_store.compute_case_snapshot_hash(
                case.question, case.evidence_texts, result.generated_answer
            )
            if anns[0].case_snapshot_hash != current_hash:
                continue  # stale -- excluded, never compared against different content
        ground_truths_by_case[case_id] = gt
        if len(anns) >= 2:
            double_reviewed += 1
            if gt.status == "agreed_review":
                agreed += 1
            elif gt.status == "unresolved_disagreement":
                unresolved += 1

    coverage = HumanGroundTruthCoverage(
        dataset_version=dataset_version,
        annotation_schema_version="annotation-v1",
        total_cases=len(annotatable_cases),
        annotated_cases=len(annotations_by_case),
        double_reviewed_cases=double_reviewed,
        agreed_cases=agreed,
        unresolved_disagreements=unresolved,
        provenance_counts=provenance_counts,
        eligible_for_evaluator_validation=sum(
            1 for gt in ground_truths_by_case.values() if gt.status != "unresolved_disagreement"
        ),
    )

    agreement_report = ground_truth.compute_inter_annotator_agreement(
        annotatable_cases,
        annotations_by_case,
        dataset_version=dataset_version,
        annotation_schema_version="annotation-v1",
        minimum_sample_size=DEFAULT_MINIMUM_SAMPLE_SIZE,
        resolutions_by_case=resolutions_by_case,
    )

    validation_report = evaluator_validation.validate_evaluator_against_ground_truth(
        results_by_case,
        ground_truths_by_case,
        dataset_version=dataset_version,
        minimum_sample_size=DEFAULT_MINIMUM_SAMPLE_SIZE,
    )

    reliability = evaluator_reliability_eligibility(
        agreement_report=agreement_report,
        evaluator_validation_report=validation_report,
        minimum_sample_size=DEFAULT_MINIMUM_SAMPLE_SIZE,
    )

    return coverage, agreement_report, validation_report, reliability


async def _run(args: argparse.Namespace) -> int:
    print("=" * 78, flush=True)
    print("EKIP -- SEMANTIC BENCHMARK (Tier 3)", flush=True)
    print("=" * 78, flush=True)

    try:
        model_provider, model_name = _require_live_model()
    except CredentialsUnavailableError as exc:
        print(f"\nBENCHMARK EXECUTION FAILED: {exc}", flush=True)
        return 1

    from app.agents.llm import get_llm

    llm = get_llm(temperature=0.0)
    runner = SemanticBenchmarkRunner(llm, model_provider=model_provider, model_name=model_name)

    answer_quality_cases = list(SYNTHETIC_ANSWER_QUALITY_CASES)
    investigation_ab_cases = list(SYNTHETIC_INVESTIGATION_AB_CASES)
    if args.limit:
        answer_quality_cases = answer_quality_cases[: args.limit]
        investigation_ab_cases = investigation_ab_cases[: args.limit]

    # Contrast cases (Priority 9 section 6) always run in full, never
    # truncated by --limit: they're a fixed, small (6-case), zero-
    # generation-cost discrimination set -- the whole point of running them
    # is the three same-question/same-evidence pairs together, and dropping
    # one side of a pair to fit a --limit would silently break that.
    answer_quality_cases += CONTRAST_ANSWER_QUALITY_CASES
    # The 3 repository-derived annotatable cases (Priority 11) -- also
    # fixed_answer, also uncapped by --limit, for the same reason as the
    # contrast cases above.
    answer_quality_cases += load_repository_derived_annotatable_cases()

    if args.repository_derived:
        repo_cases = await _fill_repository_derived_evidence(
            load_repository_derived_answer_quality_cases(limit=args.limit), args.org_slug
        )
        answer_quality_cases += repo_cases

    print(
        f"\nRunning {len(answer_quality_cases)} answer-quality case(s), "
        f"{len(investigation_ab_cases)} investigation A/B case(s)...",
        flush=True,
    )
    print(f"Model: {model_provider}/{model_name}", flush=True)

    try:
        answer_quality_results = await runner.run_answer_quality(answer_quality_cases)
        investigation_ab_results = await runner.run_investigation_ab(investigation_ab_cases)
    except Exception as exc:  # noqa: BLE001 - infrastructure failure must be reported, not raised raw
        print(f"\nBENCHMARK EXECUTION FAILED: {exc}", flush=True)
        return 1

    calibration: list[CalibrationReport] = []
    eval_confidence_calibration = calibration_from_eval_confidence_report(
        args.eval_confidence_report
    )
    if eval_confidence_calibration is not None:
        calibration.append(eval_confidence_calibration)
    calibration.append(
        CalibrationReport(
            setting_name="confidence_signal_weights",
            current_value=0.0,
            description=(
                "app.agents.confidence._SIGNAL_WEIGHTS combines top_similarity/rerank_score/"
                "source_count/historical_similarity into confidence_score."
            ),
            sample_size=0,
            minimum_sample_size=0,
            status="insufficient_data",
            rationale=(
                "_SIGNAL_WEIGHTS' own module docstring already states these are placeholder "
                "relative weights, not a tuned model (ENGINEERING_DECISIONS.md's 'Open' "
                "section: 'will be decided empirically once real retrieval data exists') -- "
                "this is an acknowledged-uncalibrated placeholder, not an intentional domain "
                "rule. It is also a 4-value weighting formula, not the single scalar this "
                "package's binary-threshold sweep methodology (calibration.sweep_binary_"
                "threshold) can examine, so it has not been swept here -- see "
                "docs/SEMANTIC_BENCHMARK.md's threshold inventory."
            ),
        )
    )
    calibration.append(
        CalibrationReport(
            setting_name="memory_relevance_threshold",
            current_value=get_settings_value("memory_relevance_threshold"),
            description=(
                "Minimum cosine relevance for a recalled memory to be injected "
                "(app.core.memory.service)."
            ),
            sample_size=0,
            minimum_sample_size=DEFAULT_MINIMUM_SAMPLE_SIZE,
            status="insufficient_data",
            rationale=(
                "Settings.memory_relevance_threshold's own field description already states "
                "it is an honestly-labelled placeholder, NOT empirically calibrated like "
                "confidence_threshold -- calibrating it needs a real memory corpus (recalled "
                "memory, relevance judgment pairs) that does not exist yet. This benchmark "
                "does not fabricate one; see docs/SEMANTIC_BENCHMARK.md."
            ),
        )
    )

    coverage, agreement_report, validation_report, reliability = _build_human_ground_truth_report(
        answer_quality_cases, answer_quality_results, dataset_version=DATASET_VERSION
    )
    if reliability is not None:
        calibration.append(reliability)

    report = runner.build_report(
        answer_quality_results=answer_quality_results,
        investigation_ab_results=investigation_ab_results,
        calibration=calibration,
        git_commit=_git_commit(),
    )
    report = report.model_copy(
        update={
            "human_ground_truth_coverage": coverage,
            "inter_annotator_agreement": agreement_report,
            "evaluator_validation": validation_report,
        }
    )

    print(f"\nDataset version: {report.execution.dataset_version}", flush=True)
    print(f"Generated at   : {report.execution.generated_at.isoformat()}", flush=True)
    _print_answer_quality(report)
    _print_investigation_ab(report)
    _print_human_ground_truth(report.human_ground_truth_coverage)
    _print_evaluator_validation(report.evaluator_validation)
    _print_calibration(report.calibration)

    print("\n" + "=" * 78, flush=True)
    print(f"Total latency        : {report.total_latency_seconds:.2f}s", flush=True)
    print(
        f"Total tokens         : {report.total_prompt_tokens + report.total_completion_tokens} "
        f"(prompt={report.total_prompt_tokens}, completion={report.total_completion_tokens})",
        flush=True,
    )
    cost = f"${report.estimated_cost_usd:.4f}" if report.estimated_cost_usd is not None else "n/a"
    print(f"Estimated cost (USD) : {cost}", flush=True)
    print(f"VERDICT: {report.verdict.upper()} -- {report.verdict_reason}", flush=True)

    args.report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"\nFull report written to: {args.report_path}", flush=True)

    return 0 if report.verdict != "regression_detected" else 1


def get_settings_value(name: str) -> float:
    return getattr(get_settings(), name)


async def _fill_repository_derived_evidence(cases, org_slug: str):
    """Run real retrieval against `org_slug`'s real ingested data to
    populate each case's `evidence_texts` -- the one place this script
    touches a live database. Returns `[]` untouched if `cases` is empty
    (e.g. `scripts/eval_confidence_dataset.json` doesn't exist).
    """
    if not cases:
        return []

    from sqlalchemy import select

    from app.database.models.tenancy_models import Organization
    from app.database.session import session_scope, set_tenant_context
    from app.retrieval import service as retrieval_service
    from app.retrieval.schemas import SearchFilters

    filled = []
    async with session_scope() as session:
        org_row = (
            await session.execute(select(Organization).where(Organization.slug == org_slug))
        ).scalar_one_or_none()
        if org_row is None:
            print(
                f"  WARNING: organization {org_slug!r} not found -- skipping "
                "repository-derived cases",
                flush=True,
            )
            return []
        await set_tenant_context(session, org_row.id)
        filters = SearchFilters(organization_id=org_row.id, permission_codes=frozenset())
        for case in cases:
            chunks = await retrieval_service.search(session, case.question, filters, top_k=5)
            filled.append(case.model_copy(update={"evidence_texts": [c.content for c in chunks]}))
    return [c for c in filled if c.evidence_texts]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run EKIP's live semantic benchmark (Tier 3: answer quality + "
        "Investigation A/B + threshold calibration)."
    )
    parser.add_argument(
        "--repository-derived",
        action="store_true",
        help="also run answer-quality cases against real retrieval over --org-slug's "
        "ingested data (requires a live database with that data already ingested)",
    )
    parser.add_argument("--org-slug", default="test-org")
    parser.add_argument("--limit", type=int, help="run at most N cases per category")
    parser.add_argument("--report-path", type=Path, default=_DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--eval-confidence-report",
        type=Path,
        default=_DEFAULT_EVAL_CONFIDENCE_REPORT,
        help="path to a scripts/eval_confidence.py report to re-express as this "
        "benchmark's confidence_threshold calibration entry (not re-run)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
