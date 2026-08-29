"""Threshold calibration analysis -- Tier 3.

Explicitly NOT a brute-force optimizer (this priority's own instruction).
For a given threshold and a set of already-computed, per-case
(predicted_value, ground_truth) pairs, sweeps a small list of candidate
values and reports precision/recall at each -- the exact "threshold = 0.60
-> precision/recall" shape the spec describes, generalizing `scripts.
eval_confidence`'s own threshold-sweep methodology (binary classification
precision/recall/f1 from a confusion matrix) into a reusable function
instead of leaving it duplicated for the next threshold that needs the
same analysis.

WHY A MINIMUM SAMPLE SIZE IS STRUCTURAL, NOT ADVISORY
    `classify` can NEVER return `"calibrated"` below `minimum_sample_size`,
    regardless of how clean the numbers look -- this is the direct fix for
    the failure mode this priority exists to prevent (a threshold declared
    "reasonable" from a handful of cases). `_MINIMUM_SAMPLE_SIZE = 20` is
    itself a judgment call, not derived from a formula; documented as such
    below rather than dressed up as principled statistics this package does
    not actually perform (no confidence interval, no significance test --
    just a sample-size floor honest about being a floor).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.evaluation.semantic.schemas import (
    AgreementReport,
    CalibrationCandidateResult,
    CalibrationReport,
    EvaluatorValidationReport,
)

#: Below this many labelled data points, a calibration result is reported
#: as `"insufficient_data"` regardless of how the numbers look. Deliberately
#: small enough to be reachable by this priority's own controlled corpus
#: (so the calibration MACHINERY can be exercised end-to-end) while still
#: being large enough that one or two atypical cases can't single-handedly
#: swing a verdict -- an explicit judgment call, not a statistically
#: derived figure. See module docstring.
DEFAULT_MINIMUM_SAMPLE_SIZE = 20

#: Two candidates must differ by at least this much on the chosen metric
#: before recommending a change away from the current value -- mirrors
#: `scripts/eval_confidence.py`'s own `_MARGIN_FOR_CHANGE = 0.05` reasoning:
#: below this, the difference is indistinguishable from one case flipping
#: categories on a small dataset, not a real signal.
DEFAULT_MARGIN_FOR_CHANGE = 0.05


def binary_precision_recall(predictions: list[tuple[bool, bool]]) -> dict[str, float | None]:
    """`predictions` is `(predicted_positive, actual_positive)` pairs.
    Returns `{"precision", "recall", "f1", "accuracy"}`, `None` for any
    metric whose denominator is zero -- never a silently wrong `0.0`."""
    tp = sum(1 for p, a in predictions if p and a)
    fp = sum(1 for p, a in predictions if p and not a)
    fn = sum(1 for p, a in predictions if not p and a)
    tn = sum(1 for p, a in predictions if not p and not a)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision and recall) else None
    total = tp + fp + fn + tn
    accuracy = (tp + tn) / total if total else None
    return {"precision": precision, "recall": recall, "f1": f1, "accuracy": accuracy}


def sweep_binary_threshold(
    *,
    setting_name: str,
    current_value: float,
    description: str,
    scored_examples: list[tuple[float, bool]],
    candidates: list[float],
    minimum_sample_size: int = DEFAULT_MINIMUM_SAMPLE_SIZE,
    margin_for_change: float = DEFAULT_MARGIN_FOR_CHANGE,
    higher_score_means_positive: bool = True,
) -> CalibrationReport:
    """Sweep `candidates` for a threshold that gates a binary decision from
    a continuous `score` (e.g. confidence, cosine relevance).
    `scored_examples` is `(score, ground_truth_is_positive)` pairs, already
    computed by the caller (this function does not run any model itself --
    see module docstring).
    """
    sample_size = len(scored_examples)
    rows: list[CalibrationCandidateResult] = []
    for candidate in candidates:
        predictions = [
            (
                (score >= candidate) if higher_score_means_positive else (score <= candidate),
                positive,
            )
            for score, positive in scored_examples
        ]
        rows.append(
            CalibrationCandidateResult(
                candidate_value=candidate, metrics=binary_precision_recall(predictions)
            )
        )

    if sample_size < minimum_sample_size:
        return CalibrationReport(
            setting_name=setting_name,
            current_value=current_value,
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            candidates=rows,
            recommended_value=None,
            status="insufficient_data",
            rationale=(
                f"only {sample_size} labelled example(s) available; "
                f"{minimum_sample_size} is the floor below which this package reports a "
                "candidate result at all, but never labels it 'calibrated' -- see "
                "calibration.py's module docstring on why this floor is a judgment call, "
                "not derived statistics."
            ),
        )

    scored_rows = [row for row in rows if row.metrics.get("f1") is not None]
    if not scored_rows:
        return CalibrationReport(
            setting_name=setting_name,
            current_value=current_value,
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            candidates=rows,
            recommended_value=None,
            status="insufficient_data",
            rationale="f1 is undefined at every candidate (the dataset has only one class) -- "
            "no threshold comparison is meaningful on this sample.",
        )

    best = max(scored_rows, key=lambda row: (row.metrics["f1"], row.candidate_value))
    current_row = next((row for row in rows if row.candidate_value == current_value), None)
    current_f1 = current_row.metrics.get("f1") if current_row else None

    if current_row is not None and (current_f1 or 0) >= (best.metrics["f1"] or 0):
        return CalibrationReport(
            setting_name=setting_name,
            current_value=current_value,
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            candidates=rows,
            recommended_value=current_value,
            status="calibrated",
            rationale=f"the current value already ties or beats every swept candidate on this "
            f"{sample_size}-example sample (f1={current_f1}).",
        )

    margin = (best.metrics["f1"] or 0) - (current_f1 or 0)
    if margin < margin_for_change:
        return CalibrationReport(
            setting_name=setting_name,
            current_value=current_value,
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            candidates=rows,
            recommended_value=current_value,
            status="provisional",
            rationale=f"candidate {best.candidate_value} scores only marginally higher than the "
            f"current default (f1 {best.metrics['f1']:.3f} vs {current_f1}, a {margin:.3f} "
            f"margin on {sample_size} examples) -- below the {margin_for_change} margin this "
            "package treats as a real signal rather than sample noise.",
        )

    return CalibrationReport(
        setting_name=setting_name,
        current_value=current_value,
        description=description,
        sample_size=sample_size,
        minimum_sample_size=minimum_sample_size,
        candidates=rows,
        recommended_value=best.candidate_value,
        status="provisional",
        rationale=f"candidate {best.candidate_value} outperforms the current default "
        f"(f1 {best.metrics['f1']:.3f} vs {current_f1}) on this {sample_size}-example sample -- "
        "reported as 'provisional', not 'calibrated': a single benchmark run, however clean, "
        "is not enough evidence to change a production default on its own (see "
        "docs/SEMANTIC_BENCHMARK.md's calibration methodology).",
    )


def calibration_from_eval_confidence_report(
    report_path: Path,
    *,
    minimum_sample_size: int = DEFAULT_MINIMUM_SAMPLE_SIZE,
    margin_for_change: float = DEFAULT_MARGIN_FOR_CHANGE,
) -> CalibrationReport | None:
    """Re-express `scripts/eval_confidence.py`'s own last written report
    (its precomputed `threshold_sweep`, against real `test-org` data) as a
    `CalibrationReport` for `Settings.confidence_threshold` -- REUSING that
    harness's real prior measurement, not re-running it (a full run makes a
    live LLM call per question and per grounding trace; this package's own
    report should not silently double that cost every time it runs).

    Returns `None` if `report_path` doesn't exist -- callers treat that as
    "no prior eval_confidence.py run available," the same honest-empty
    convention `fixtures.load_repository_derived_answer_quality_cases`
    uses.
    """
    if not report_path.exists():
        return None
    data = json.loads(report_path.read_text(encoding="utf-8"))

    current_value = data["current_default_threshold"]
    sweep = data.get("threshold_sweep", [])
    candidates = [
        CalibrationCandidateResult(
            candidate_value=row["threshold"],
            metrics={
                "precision": row.get("precision"),
                "recall": row.get("recall"),
                "f1": row.get("f1"),
                "accuracy": row.get("accuracy"),
            },
        )
        for row in sweep
    ]
    current_row = next((row for row in sweep if row["threshold"] == current_value), None)
    sample_size = sum(current_row.get(k, 0) for k in ("tp", "fp", "fn", "tn")) if current_row else 0
    negative_examples = (current_row.get("fp", 0) + current_row.get("tn", 0)) if current_row else 0
    description = (
        "Settings.confidence_threshold -- gates the answer-vs-investigate routing "
        "decision (app.agents.confidence.evaluate_confidence / agents.graph."
        "_route_after_confidence). Re-expressed here from scripts/eval_confidence.py's "
        f"last written report ({report_path}), generated_at={data.get('generated_at')!r}, "
        f"against real ingested data in organization {data.get('organization', {}).get('slug')!r} "
        "-- not re-run by this package."
    )

    best = data.get("best_threshold_metrics") or {}
    best_value = data.get("best_threshold")
    current_f1 = current_row.get("f1") if current_row else None

    if sample_size < minimum_sample_size:
        status, rationale = (
            "insufficient_data",
            f"only {sample_size} labelled example(s) in the confusion matrix at the current "
            f"threshold ({minimum_sample_size} is this package's floor) -- see the module "
            "docstring on why that floor is a judgment call.",
        )
    elif negative_examples == 0:
        status, rationale = (
            "insufficient_data",
            "the referenced run's confusion matrix has zero negative-class (ambiguous/"
            "no-information) examples at the current threshold -- precision/recall are "
            "well-defined but the sample cannot demonstrate the threshold correctly REJECTS "
            "anything, only that it doesn't wrongly reject the positive class.",
        )
    elif best_value == current_value or (best.get("f1") or 0) <= (current_f1 or 0):
        status, rationale = (
            "calibrated",
            f"the current default ({current_value}) already ties or beats every swept "
            f"candidate on this {sample_size}-example real-data sample (f1={current_f1}).",
        )
    elif (best.get("f1") or 0) - (current_f1 or 0) < margin_for_change:
        status, rationale = (
            "provisional",
            f"candidate {best_value} scores only marginally higher than the current default "
            f"(f1 {best.get('f1')} vs {current_f1}) -- below the {margin_for_change} margin "
            "this package treats as a real signal.",
        )
    else:
        status, rationale = (
            "provisional",
            f"candidate {best_value} outperforms the current default (f1 {best.get('f1')} vs "
            f"{current_f1}) on this real-data sample -- 'provisional', not 'calibrated': see "
            "scripts/eval_confidence.py's own docstring on re-running against a larger/"
            "refreshed dataset before changing the production default.",
        )

    return CalibrationReport(
        setting_name="confidence_threshold",
        current_value=current_value,
        description=description,
        sample_size=sample_size,
        minimum_sample_size=minimum_sample_size,
        candidates=candidates,
        recommended_value=best_value if status == "provisional" else current_value,
        status=status,
        rationale=rationale,
    )


def fixed_domain_rule(setting_name: str, current_value: float, rationale: str) -> CalibrationReport:
    """For a threshold that is an intentional architectural/domain
    decision, not an empirical parameter -- e.g. a hard traversal-depth
    cap, a signal-weighting scheme mirroring a documented design choice.
    Included in a calibration report so the inventory is complete (every
    threshold examined, per this priority's requirement) without pretending
    a sweep was run against something that was never meant to be tuned by
    one.
    """
    return CalibrationReport(
        setting_name=setting_name,
        current_value=current_value,
        description=rationale,
        sample_size=0,
        minimum_sample_size=0,
        candidates=[],
        recommended_value=current_value,
        status="intentionally_fixed_domain_rule",
        rationale=rationale,
    )


#: The outcome classes an evaluator-reliability judgment should see actual
#: human-labelled examples of before it's trusted -- deliberately the two
#: DANGEROUS ones (section 10: hallucination and lazy refusal are the
#: severe-failure classes), not all five. A corpus with only "correct"
#: examples cannot demonstrate the evaluator catches the mistakes that
#: matter most, however large it is.
_REQUIRED_COVERAGE_CLASSES = frozenset({"critical_failure", "incorrect_refusal"})


def evaluator_reliability_eligibility(
    *,
    agreement_report: AgreementReport,
    evaluator_validation_report: EvaluatorValidationReport,
    minimum_sample_size: int = DEFAULT_MINIMUM_SAMPLE_SIZE,
) -> CalibrationReport:
    """Priority 11 section 14: connects human ground truth to this
    package's existing calibration architecture, answering a DIFFERENT
    question than every other entry in this module -- not "what should
    threshold X be set to," but "is the semantic evaluator itself reliable
    enough to be trusted for future calibration work at all." Reused as a
    `CalibrationReport` on purpose (same shape, same honest-status
    discipline) rather than inventing a parallel report type for what is
    still fundamentally the same claim: a measurement, a sample size, and a
    status that can never overstate what the sample supports.

    Gating, most to least specific reason for NOT being `calibrated`:
      1. `evaluator_validation_report.sample_size < minimum_sample_size` ->
         `insufficient_data` -- not enough human-labelled cases exist at
         all.
      2. `agreement_report.status != "computed"` -> `insufficient_agreement`
         -- even if the evaluator-vs-human sample were large enough, the
         HUMAN labels themselves aren't demonstrated to be reproducible
         (too few double-reviewed cases, or reviewers don't agree enough)
         -- trusting the evaluator against unreliable ground truth would be
         circular.
      3. Missing coverage of `_REQUIRED_COVERAGE_CLASSES` in the human
         labels actually compared -> `insufficient_class_coverage` -- a
         clean-looking accuracy number that never included a single
         hallucination or lazy-refusal example says nothing about whether
         the evaluator catches either.
      4. Any severe disagreement present (`evaluator_validation_report.
         severe_disagreements`) -> stays `provisional`, never `calibrated`
         -- section 10's own point: a severe miss must be visible, not
         outvoted by an otherwise-clean aggregate rate.
      5. Otherwise -> `provisional` (never `calibrated` from one
         measurement pass -- the same "a single benchmark run isn't enough
         evidence" discipline every other entry in this module already
         applies).
    """
    sample_size = evaluator_validation_report.sample_size
    description = (
        "Whether app.evaluation.semantic's LLM answer-quality judge is reliable enough, "
        "against human ground truth, to be trusted for future threshold-calibration work."
    )

    if sample_size < minimum_sample_size:
        return CalibrationReport(
            setting_name="semantic_evaluator_reliability",
            current_value=float(sample_size),
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            status="insufficient_data",
            rationale=(
                f"only {sample_size} case(s) have both an evaluator result and a resolved "
                f"human ground truth label -- {minimum_sample_size} is this package's floor. "
                "See docs/SEMANTIC_BENCHMARK.md's calibration methodology."
            ),
        )

    if agreement_report.status != "computed":
        return CalibrationReport(
            setting_name="semantic_evaluator_reliability",
            current_value=float(sample_size),
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            status="insufficient_agreement",
            rationale=(
                "the human ground truth itself is not yet demonstrated reproducible: "
                f"{agreement_report.rationale} Trusting the evaluator against ground truth "
                "that hasn't itself been shown reliable would be circular."
            ),
        )

    observed_classes = {
        cell.human_label for cell in evaluator_validation_report.outcome_confusion_matrix
    }
    missing = _REQUIRED_COVERAGE_CLASSES - observed_classes
    if missing:
        return CalibrationReport(
            setting_name="semantic_evaluator_reliability",
            current_value=float(sample_size),
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            status="insufficient_class_coverage",
            rationale=(
                f"human ground truth compared against the evaluator never included a "
                f"labelled example of: {sorted(missing)} -- a clean aggregate rate without "
                "these says nothing about whether the evaluator catches the failure modes "
                "this benchmark most exists to catch."
            ),
        )

    if evaluator_validation_report.severe_disagreements:
        return CalibrationReport(
            setting_name="semantic_evaluator_reliability",
            current_value=float(sample_size),
            description=description,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            status="provisional",
            rationale=(
                f"{len(evaluator_validation_report.severe_disagreements)} severe "
                "disagreement(s) between the evaluator and human ground truth -- see the "
                "Evaluator vs Human Validation report section. Never 'calibrated' while any "
                "severe disagreement is present, regardless of aggregate agreement rate."
            ),
        )

    return CalibrationReport(
        setting_name="semantic_evaluator_reliability",
        current_value=float(sample_size),
        description=description,
        sample_size=sample_size,
        minimum_sample_size=minimum_sample_size,
        status="provisional",
        rationale=(
            f"{sample_size} case(s), agreement/coverage checks passed, no severe "
            "disagreements -- a genuinely clean result, but one measurement pass is not "
            "enough evidence to call the evaluator 'calibrated'; repeated, larger, "
            "independently-reviewed passes would be."
        ),
    )
