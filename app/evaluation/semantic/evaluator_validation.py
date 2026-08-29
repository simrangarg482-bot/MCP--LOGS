"""Compares automated evaluator output (`AnswerQualityResult`) against
human ground truth (`CaseGroundTruth`) -- Priority 11 section 9. Every
function in this module is plain, deterministic Python: no LLM call
anywhere, per that section's explicit "do not ask an LLM to judge whether
its own output matches human labels."

SEVERITY MODEL (section 10)
    Not every disagreement is equally dangerous. `_classify_severity`
    implements the four worked examples from that section as an explicit,
    documented rule table -- not a weighted score (the section explicitly
    says "do not necessarily invent weighted metrics"), just a
    classification that keeps the most dangerous mismatches from being
    buried inside one aggregate accuracy number:

      CRITICAL -- human says `critical_failure` (a real hallucination) but
      the evaluator called it `correct`/`partially_correct`: the evaluator
      would let a hallucination through as if it were fine. This is
      section 10's first two examples (worded there as "Human: no_answer /
      Evaluator: correct substantive answer" and "Human: critical_failure /
      Evaluator: correct") -- both describe the same danger: the evaluator
      normalizing a fabricated answer.

      CRITICAL -- human says `incorrect_refusal` (evidence was actually
      sufficient) but the evaluator called it `correct`: the evaluator
      would rate a lazy refusal as a good outcome.

      HIGH -- human says `overconfident` but the evaluator called it
      `correct`: the evaluator missed the system overstating certainty.

      Everything else that disagrees (e.g. human `correct` vs. evaluator
      `partially_correct`, section 10's own explicitly "Lower severity"
      example) is counted in the confusion matrix and agreement rate but
      NOT flagged as severe.
"""

from __future__ import annotations

from app.evaluation.semantic.schemas import (
    AnswerOutcomeCorrectness,
    AnswerQualityResult,
    CaseGroundTruth,
    ClassMetric,
    ConfusionMatrixCell,
    EvaluatorValidationReport,
    SevereDisagreement,
)

_CRITICAL_HALLUCINATION_MISSES = {"correct", "partially_correct"}


def _classify_severity(
    human_outcome: AnswerOutcomeCorrectness, evaluator_outcome: AnswerOutcomeCorrectness
) -> tuple[str, str] | None:
    """Returns `(severity, explanation)` or `None` if this specific
    mismatch isn't one of the named dangerous shapes -- see module
    docstring for the rule table."""
    if human_outcome == evaluator_outcome:
        return None
    if human_outcome == "critical_failure" and evaluator_outcome in _CRITICAL_HALLUCINATION_MISSES:
        return (
            "critical",
            "human identified a hallucination (evidence had no real bearing on the question) "
            f"but the evaluator scored it {evaluator_outcome!r} -- this would let a fabricated "
            "answer through as if it were fine.",
        )
    if human_outcome == "incorrect_refusal" and evaluator_outcome == "correct":
        return (
            "critical",
            "human identified a lazy/incorrect refusal (the evidence was actually sufficient) "
            "but the evaluator scored it 'correct' -- this would rate declining to help as a "
            "good outcome.",
        )
    if human_outcome == "overconfident" and evaluator_outcome == "correct":
        return (
            "high",
            "human identified overconfidence (partial evidence presented with unwarranted "
            "certainty) but the evaluator scored it 'correct' -- this would miss the system "
            "overstating what it actually knows.",
        )
    return None


def _class_metrics(matrix: dict[tuple[str, str], int], labels: list[str]) -> list[ClassMetric]:
    metrics = []
    for label in labels:
        tp = matrix.get((label, label), 0)
        fp = sum(count for (h, e), count in matrix.items() if e == label and h != label)
        fn = sum(count for (h, e), count in matrix.items() if h == label and e != label)
        support = sum(count for (h, e), count in matrix.items() if h == label)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        f1 = (2 * precision * recall / (precision + recall)) if (precision and recall) else None
        metrics.append(
            ClassMetric(label=label, support=support, precision=precision, recall=recall, f1=f1)
        )
    return metrics


def validate_evaluator_against_ground_truth(
    results_by_case: dict[str, AnswerQualityResult],
    ground_truths_by_case: dict[str, CaseGroundTruth],
    *,
    dataset_version: str,
    minimum_sample_size: int,
) -> EvaluatorValidationReport:
    """The central Priority 11 computation. Only cases present in BOTH
    `results_by_case` (the evaluator actually judged them, no error) AND
    `ground_truths_by_case` with a resolved status (`single_review`/
    `agreed_review`/`resolved_disagreement` -- NEVER
    `unresolved_disagreement`, section 7's explicit requirement) are
    compared; everything else is counted in `excluded_case_count`, not
    silently dropped.
    """
    mode_matches = 0
    mode_total = 0
    outcome_pairs: list[tuple[str, str]] = []
    matrix: dict[tuple[str, str], int] = {}
    severe: list[SevereDisagreement] = []
    excluded = 0

    for case_id, ground_truth in ground_truths_by_case.items():
        result = results_by_case.get(case_id)
        if result is None or result.judgement is None:
            excluded += 1
            continue
        if (
            ground_truth.status == "unresolved_disagreement"
            or ground_truth.final_observed_mode is None
        ):
            excluded += 1
            continue
        if result.outcome_correctness is None or ground_truth.final_outcome is None:
            excluded += 1
            continue

        mode_total += 1
        if result.judgement.observed_answer_mode == ground_truth.final_observed_mode:
            mode_matches += 1

        human_outcome = ground_truth.final_outcome
        evaluator_outcome = result.outcome_correctness
        outcome_pairs.append((human_outcome, evaluator_outcome))
        matrix[(human_outcome, evaluator_outcome)] = (
            matrix.get((human_outcome, evaluator_outcome), 0) + 1
        )

        severity = _classify_severity(human_outcome, evaluator_outcome)
        if severity is not None:
            level, explanation = severity
            severe.append(
                SevereDisagreement(
                    case_id=case_id,
                    human_outcome=human_outcome,
                    evaluator_outcome=evaluator_outcome,
                    severity=level,
                    explanation=explanation,
                )
            )

    sample_size = len(outcome_pairs)
    answer_mode_agreement_rate = (mode_matches / mode_total) if mode_total else None
    outcome_matches = sum(1 for h, e in outcome_pairs if h == e)
    outcome_agreement_rate = (outcome_matches / sample_size) if sample_size else None

    labels = sorted({h for h, _ in outcome_pairs} | {e for _, e in outcome_pairs})
    confusion_cells = [
        ConfusionMatrixCell(human_label=h, evaluator_label=e, count=count)
        for (h, e), count in sorted(matrix.items())
    ]
    class_metrics = _class_metrics(matrix, labels)

    if sample_size < minimum_sample_size:
        return EvaluatorValidationReport(
            dataset_version=dataset_version,
            sample_size=sample_size,
            minimum_sample_size=minimum_sample_size,
            answer_mode_agreement_rate=answer_mode_agreement_rate,
            outcome_agreement_rate=outcome_agreement_rate,
            outcome_confusion_matrix=confusion_cells,
            outcome_class_metrics=class_metrics,
            severe_disagreements=severe,
            excluded_case_count=excluded,
            status="insufficient_data",
            rationale=(
                f"only {sample_size} case(s) have both an evaluator result and a resolved "
                f"human ground truth -- {minimum_sample_size} is this package's floor below "
                "which agreement/precision/recall are reported but not claimed as "
                "statistically meaningful. Metrics above are still real measurements on this "
                "small sample, not fabricated -- just not evidence of general evaluator "
                "reliability yet."
            ),
        )

    return EvaluatorValidationReport(
        dataset_version=dataset_version,
        sample_size=sample_size,
        minimum_sample_size=minimum_sample_size,
        answer_mode_agreement_rate=answer_mode_agreement_rate,
        outcome_agreement_rate=outcome_agreement_rate,
        outcome_confusion_matrix=confusion_cells,
        outcome_class_metrics=class_metrics,
        severe_disagreements=severe,
        excluded_case_count=excluded,
        status="computed",
        rationale=(
            f"{sample_size} case(s) compared against resolved human ground truth: "
            f"answer-mode agreement {answer_mode_agreement_rate:.2f}, outcome agreement "
            f"{outcome_agreement_rate:.2f}, {len(severe)} severe disagreement(s)."
        ),
    )
