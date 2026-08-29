"""Resolves raw `AnnotationDecision` records into `CaseGroundTruth`, and
computes inter-annotator agreement -- Priority 11 sections 7/8.

THE TWO-REVIEWER MODEL (section 7's own framing, taken literally)
    This module supports exactly the shape section 7 describes: at most
    two INDEPENDENT annotations per case, plus an optional resolution.
    `resolve_ground_truth` takes the chronologically first two annotations
    as "the independent pair" if more than two exist -- a documented
    simplification, not a silent one: this codebase's initial annotation
    corpus never needs a third independent reviewer, and inventing a
    multi-rater agreement statistic (Fleiss' kappa, etc.) nobody asked for
    would be exactly the "add sophisticated statistics merely to look
    advanced" section 8 warns against.

WHY OUTCOME IS DERIVED, NEVER A SEPARATE HAND-PICKED LABEL
    See `AnnotationDecision`'s own docstring in `schemas.py`:
    `derive_outcome_for_annotation` applies the SAME `outcome.
    classify_outcome_correctness` function the automated evaluator's
    outcome is computed through, so a human and the evaluator are held to
    the identical decision rule.
"""

from __future__ import annotations

import math

from app.evaluation.semantic.outcome import classify_outcome_correctness
from app.evaluation.semantic.schemas import (
    AgreementReport,
    AnnotationDecision,
    AnswerOutcomeCorrectness,
    AnswerQualityCase,
    CaseGroundTruth,
    ExpectedAnswerMode,
    ObservedAnswerMode,
    ResolutionAnnotation,
)


def derive_outcome_for_annotation(
    expected_answer_mode: ExpectedAnswerMode, observed_mode: ObservedAnswerMode
) -> AnswerOutcomeCorrectness | None:
    """`None` only when `expected_answer_mode == "unlabeled"` -- the same
    honest exclusion `outcome.classify_outcome_correctness` already applies
    to the automated evaluator's own results."""
    return classify_outcome_correctness(expected_answer_mode, observed_mode)


def resolve_ground_truth(
    case: AnswerQualityCase,
    annotations: list[AnnotationDecision],
    resolution: ResolutionAnnotation | None = None,
) -> CaseGroundTruth:
    """Pure function: given one case's `expected_answer_mode` and every
    annotation recorded against it (plus an optional resolution), returns
    the resolved `CaseGroundTruth`. Raises `ValueError` if `annotations` is
    empty -- callers should not construct ground truth for an unannotated
    case at all (see `HumanGroundTruthCoverage`'s own "annotated_cases"
    counting instead).
    """
    if not annotations:
        raise ValueError(f"cannot resolve ground truth for {case.id!r} with zero annotations")

    ordered = sorted(annotations, key=lambda a: a.annotated_at)
    independent_pair = ordered[:2]

    if len(independent_pair) == 1:
        only = independent_pair[0]
        outcome = derive_outcome_for_annotation(case.expected_answer_mode, only.observed_mode)
        return CaseGroundTruth(
            case_id=case.id,
            dataset_version=only.dataset_version,
            annotations=ordered,
            resolution=resolution,
            status="single_review",
            final_observed_mode=only.observed_mode,
            final_outcome=outcome,
        )

    first, second = independent_pair
    if first.observed_mode == second.observed_mode:
        outcome = derive_outcome_for_annotation(case.expected_answer_mode, first.observed_mode)
        return CaseGroundTruth(
            case_id=case.id,
            dataset_version=first.dataset_version,
            annotations=ordered,
            resolution=resolution,
            status="agreed_review",
            final_observed_mode=first.observed_mode,
            final_outcome=outcome,
        )

    # Disagreement.
    if resolution is not None:
        outcome = derive_outcome_for_annotation(case.expected_answer_mode, resolution.observed_mode)
        return CaseGroundTruth(
            case_id=case.id,
            dataset_version=first.dataset_version,
            annotations=ordered,
            resolution=resolution,
            status="resolved_disagreement",
            final_observed_mode=resolution.observed_mode,
            final_outcome=outcome,
        )

    return CaseGroundTruth(
        case_id=case.id,
        dataset_version=first.dataset_version,
        annotations=ordered,
        resolution=None,
        status="unresolved_disagreement",
        final_observed_mode=None,  # never a guessed truth -- section 7
        final_outcome=None,
    )


def _cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Standard two-rater, multi-class Cohen's kappa. `pairs` is
    `(rater_a_label, rater_b_label)` per case. `None` if the expected
    agreement by chance is 1.0 (every rater used exactly one, identical
    label -- kappa is undefined, not "1.0 by default").
    """
    n = len(pairs)
    if n == 0:
        return None
    labels = sorted({label for pair in pairs for label in pair})
    row_counts = {label: 0 for label in labels}
    col_counts = {label: 0 for label in labels}
    agree = 0
    for a, b in pairs:
        row_counts[a] += 1
        col_counts[b] += 1
        if a == b:
            agree += 1
    p_observed = agree / n
    p_expected = sum((row_counts[label] / n) * (col_counts[label] / n) for label in labels)
    if math.isclose(p_expected, 1.0):
        return None
    return (p_observed - p_expected) / (1 - p_expected)


def compute_inter_annotator_agreement(
    cases_by_id: dict[str, AnswerQualityCase],
    annotations_by_case: dict[str, list[AnnotationDecision]],
    *,
    dataset_version: str,
    annotation_schema_version: str,
    minimum_sample_size: int,
    resolutions_by_case: dict[str, ResolutionAnnotation] | None = None,
) -> AgreementReport:
    """Section 8. Only cases with an independent pair (>=2 annotations)
    count toward `double_reviewed_case_count`/agreement/kappa --
    single-reviewed cases contribute nothing here (there is no second
    opinion to compare against yet).

    `resolutions_by_case`, when given, is consulted the same way
    `resolve_ground_truth` itself does -- a disagreement with a recorded
    resolution is `resolved_disagreement`, not `unresolved_disagreement`.
    Omitting it (the default) is only correct for a caller that genuinely
    has no resolutions to check; passing an empty dict would silently
    undercount resolutions exactly the way this function's first version
    did, so callers with a real annotation store should always look
    resolutions up and pass them.
    """
    resolutions_by_case = resolutions_by_case or {}
    reviewed_case_count = sum(1 for anns in annotations_by_case.values() if anns)
    pairs: list[tuple[str, str]] = []
    agreed = 0
    disagreed = 0
    unresolved = 0

    for case_id, anns in annotations_by_case.items():
        if len(anns) < 2:
            continue
        case = cases_by_id.get(case_id)
        if case is None:
            continue
        ground_truth = resolve_ground_truth(case, anns, resolutions_by_case.get(case_id))
        ordered = sorted(anns, key=lambda a: a.annotated_at)[:2]
        pairs.append((ordered[0].observed_mode, ordered[1].observed_mode))
        if ground_truth.status == "agreed_review":
            agreed += 1
        elif ground_truth.status == "unresolved_disagreement":
            disagreed += 1
            unresolved += 1
        else:  # resolved_disagreement -- still counted as a disagreement at the review level
            disagreed += 1

    double_reviewed_case_count = len(pairs)
    raw_agreement_rate = (
        (agreed / double_reviewed_case_count) if double_reviewed_case_count else None
    )

    if double_reviewed_case_count < minimum_sample_size:
        return AgreementReport(
            dataset_version=dataset_version,
            annotation_schema_version=annotation_schema_version,
            reviewed_case_count=reviewed_case_count,
            double_reviewed_case_count=double_reviewed_case_count,
            agreed_case_count=agreed,
            disagreed_case_count=disagreed,
            unresolved_disagreement_count=unresolved,
            raw_agreement_rate=raw_agreement_rate,
            cohens_kappa=None,
            minimum_sample_size=minimum_sample_size,
            status="insufficient_data",
            rationale=(
                f"only {double_reviewed_case_count} double-reviewed case(s) -- "
                f"{minimum_sample_size} is this package's floor below which inter-annotator "
                "agreement is reported but never treated as statistically meaningful (see "
                "app.evaluation.semantic.calibration's identical reasoning for its own floor)."
            ),
        )

    kappa = _cohens_kappa(pairs)
    return AgreementReport(
        dataset_version=dataset_version,
        annotation_schema_version=annotation_schema_version,
        reviewed_case_count=reviewed_case_count,
        double_reviewed_case_count=double_reviewed_case_count,
        agreed_case_count=agreed,
        disagreed_case_count=disagreed,
        unresolved_disagreement_count=unresolved,
        raw_agreement_rate=raw_agreement_rate,
        cohens_kappa=kappa,
        minimum_sample_size=minimum_sample_size,
        status="computed",
        rationale=(
            f"{double_reviewed_case_count} double-reviewed cases, "
            f"{agreed} agreed, {disagreed} disagreed ({unresolved} unresolved) -- "
            f"raw agreement {raw_agreement_rate:.2f}"
            + (f", Cohen's kappa {kappa:.2f}" if kappa is not None else ", kappa undefined")
            + "."
        ),
    )
