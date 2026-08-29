"""CLI-assisted human annotation workflow for `app.evaluation.semantic`'s
ground-truth layer (Priority 11).

WHY A CLI, NOT AN API/DATABASE (section 13)
    See `app.evaluation.semantic.annotation_store`'s own module docstring
    for the full reasoning -- this mirrors `scripts/run_evaluation.py`/
    `scripts/eval_confidence.py`/`scripts/run_semantic_evaluation.py`'s
    existing convention (a local script, no server, no auth surface,
    output reviewed the same way any other repository change is).

SUBCOMMANDS
    list       -- cases in the annotatable corpus and their review status
    show       -- one case's question/evidence/candidate answer, for a
                  reviewer to actually read before judging
    annotate   -- record one independent annotation
    resolve    -- record a resolution for a disagreed case (never edits
                  the original two annotations)
    status     -- `HumanGroundTruthCoverage` summary
    agreement  -- inter-annotator `AgreementReport`

RUN
    python scripts/annotate_semantic_cases.py list
    python scripts/annotate_semantic_cases.py show contrast-a-correct-refusal
    python scripts/annotate_semantic_cases.py annotate contrast-a-correct-refusal \\
        --reviewer reviewer-a --observed-mode no_answer \\
        --rationale "Evidence has no root cause; declining is correct." \\
        --provenance synthetic_controlled_annotation
    python scripts/annotate_semantic_cases.py resolve contrast-x \\
        --resolver reviewer-c --observed-mode no_answer --rationale "..."
    python scripts/annotate_semantic_cases.py status
    python scripts/annotate_semantic_cases.py agreement
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.evaluation.semantic import annotation_store, ground_truth  # noqa: E402
from app.evaluation.semantic.calibration import DEFAULT_MINIMUM_SAMPLE_SIZE  # noqa: E402
from app.evaluation.semantic.fixtures import (  # noqa: E402
    DATASET_VERSION,
    load_annotatable_answer_quality_cases,
)
from app.evaluation.semantic.schemas import (  # noqa: E402
    AnnotationDecision,
    HumanGroundTruthCoverage,
    ObservedAnswerMode,
    ResolutionAnnotation,
)

_OBSERVED_MODES: tuple[ObservedAnswerMode, ...] = (
    "substantive_answer",
    "qualified_answer",
    "no_answer",
)


def _cases_by_id() -> dict:
    return {case.id: case for case in load_annotatable_answer_quality_cases()}


def _annotations_by_case(dataset_version: str) -> dict[str, list[AnnotationDecision]]:
    by_case: dict[str, list[AnnotationDecision]] = {}
    for annotation in annotation_store.load_annotations(dataset_version):
        by_case.setdefault(annotation.case_id, []).append(annotation)
    return by_case


def _resolutions_by_case(dataset_version: str) -> dict[str, ResolutionAnnotation]:
    return {
        resolution.case_id: resolution
        for resolution in annotation_store.load_resolutions(dataset_version)
    }


def _cmd_list(args: argparse.Namespace) -> int:
    cases = _cases_by_id()
    annotations = _annotations_by_case(args.dataset_version)
    print(f"Annotatable cases (dataset_version={args.dataset_version!r}):", flush=True)
    for case_id, case in sorted(cases.items()):
        count = len(annotations.get(case_id, []))
        status = (
            "unannotated"
            if count == 0
            else "single_review"
            if count == 1
            else f"{count} annotations"
        )
        print(
            f"  {case_id:<32} provenance={case.provenance:<20} "
            f"expected={case.expected_answer_mode:<16} {status}",
            flush=True,
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    case = _cases_by_id().get(args.case_id)
    if case is None:
        print(f"no annotatable case with id {args.case_id!r}", flush=True)
        return 1
    print(f"case_id: {case.id}", flush=True)
    print(f"provenance: {case.provenance}", flush=True)
    print(f"expected_answer_mode: {case.expected_answer_mode}", flush=True)
    print(f"\nquestion:\n  {case.question}", flush=True)
    print("\nevidence:", flush=True)
    if case.evidence_texts:
        for i, text in enumerate(case.evidence_texts, start=1):
            print(f"  [{i}] {text}", flush=True)
    else:
        print("  (none supplied)", flush=True)
    print(f"\ncandidate answer (fixed_answer):\n  {case.fixed_answer}", flush=True)
    existing = _annotations_by_case(args.dataset_version).get(case.id, [])
    if existing:
        print(f"\n{len(existing)} existing annotation(s):", flush=True)
        for annotation in existing:
            print(
                f"  reviewer={annotation.reviewer_id} mode={annotation.observed_mode} "
                f"provenance={annotation.provenance}",
                flush=True,
            )
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    case = _cases_by_id().get(args.case_id)
    if case is None:
        print(f"no annotatable case with id {args.case_id!r}", flush=True)
        return 1
    if case.fixed_answer is None:
        print(f"case {args.case_id!r} has no fixed_answer -- not annotatable", flush=True)
        return 1

    snapshot_hash = annotation_store.case_snapshot_hash_for(case, case.fixed_answer)
    dimension_ratings = {}
    for entry in args.dimension or []:
        key, _, value = entry.partition("=")
        if value not in ("good", "acceptable", "poor"):
            print(f"invalid dimension rating {entry!r} -- must be good|acceptable|poor", flush=True)
            return 1
        dimension_ratings[key] = value

    annotation = AnnotationDecision(
        case_id=case.id,
        dataset_version=args.dataset_version,
        case_snapshot_hash=snapshot_hash,
        reviewer_id=args.reviewer,
        provenance=args.provenance,
        annotated_at=datetime.now(UTC),
        observed_mode=args.observed_mode,
        dimension_ratings=dimension_ratings,
        rationale=args.rationale,
        usable_for_calibration=not args.unusable,
    )
    try:
        annotation_store.save_annotation(annotation)
    except annotation_store.DuplicateAnnotationError as exc:
        print(f"REJECTED: {exc}", flush=True)
        return 1

    derived_outcome = ground_truth.derive_outcome_for_annotation(
        case.expected_answer_mode, annotation.observed_mode
    )
    print(
        f"Recorded annotation: {case.id} by {args.reviewer} -> observed_mode="
        f"{args.observed_mode}, derived_outcome={derived_outcome}",
        flush=True,
    )
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    case = _cases_by_id().get(args.case_id)
    if case is None:
        print(f"no annotatable case with id {args.case_id!r}", flush=True)
        return 1
    existing = _annotations_by_case(args.dataset_version).get(case.id, [])
    if len(existing) < 2:
        print(
            f"case {args.case_id!r} has {len(existing)} annotation(s) -- resolution requires "
            "at least 2 (a disagreement to resolve)",
            flush=True,
        )
        return 1
    ordered = sorted(existing, key=lambda a: a.annotated_at)[:2]
    if ordered[0].observed_mode == ordered[1].observed_mode:
        print(
            f"case {args.case_id!r}'s first two annotations already agree -- nothing to resolve",
            flush=True,
        )
        return 1

    snapshot_hash = annotation_store.case_snapshot_hash_for(case, case.fixed_answer)
    resolution = ResolutionAnnotation(
        case_id=case.id,
        dataset_version=args.dataset_version,
        case_snapshot_hash=snapshot_hash,
        reviewer_id=args.resolver,
        provenance=args.provenance,
        annotated_at=datetime.now(UTC),
        observed_mode=args.observed_mode,
        rationale=args.rationale,
        resolved_annotation_ids=[f"{a.reviewer_id}:{a.annotated_at.isoformat()}" for a in ordered],
    )
    annotation_store.save_resolution(resolution)
    print(f"Recorded resolution: {case.id} by {args.resolver} -> {args.observed_mode}", flush=True)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cases = _cases_by_id()
    annotations = _annotations_by_case(args.dataset_version)
    resolutions = _resolutions_by_case(args.dataset_version)

    annotated = 0
    double_reviewed = 0
    agreed = 0
    unresolved = 0
    eligible = 0
    provenance_counts: dict[str, int] = {}

    for case_id, case in cases.items():
        anns = annotations.get(case_id, [])
        if not anns:
            continue
        annotated += 1
        provenance_counts[case.provenance] = provenance_counts.get(case.provenance, 0) + 1
        if len(anns) >= 2:
            double_reviewed += 1
            resolution = resolutions.get(case_id)
            result = ground_truth.resolve_ground_truth(case, anns, resolution)
            if result.status == "agreed_review":
                agreed += 1
                eligible += 1
            elif result.status == "resolved_disagreement":
                eligible += 1
            elif result.status == "unresolved_disagreement":
                unresolved += 1
        else:
            eligible += 1  # single_review -- usable, lower confidence

    coverage = HumanGroundTruthCoverage(
        dataset_version=args.dataset_version,
        annotation_schema_version="annotation-v1",
        total_cases=len(cases),
        annotated_cases=annotated,
        double_reviewed_cases=double_reviewed,
        agreed_cases=agreed,
        unresolved_disagreements=unresolved,
        provenance_counts=provenance_counts,
        eligible_for_evaluator_validation=eligible,
    )
    print(coverage.model_dump_json(indent=2), flush=True)
    return 0


def _cmd_agreement(args: argparse.Namespace) -> int:
    cases = _cases_by_id()
    annotations = _annotations_by_case(args.dataset_version)
    resolutions = _resolutions_by_case(args.dataset_version)
    report = ground_truth.compute_inter_annotator_agreement(
        cases,
        annotations,
        dataset_version=args.dataset_version,
        annotation_schema_version="annotation-v1",
        minimum_sample_size=DEFAULT_MINIMUM_SAMPLE_SIZE,
        resolutions_by_case=resolutions,
    )
    print(report.model_dump_json(indent=2), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset-version", default=DATASET_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list").set_defaults(func=_cmd_list)

    show = subparsers.add_parser("show")
    show.add_argument("case_id")
    show.set_defaults(func=_cmd_show)

    annotate = subparsers.add_parser("annotate")
    annotate.add_argument("case_id")
    annotate.add_argument("--reviewer", required=True)
    annotate.add_argument("--observed-mode", required=True, choices=_OBSERVED_MODES)
    annotate.add_argument("--rationale", required=True)
    annotate.add_argument(
        "--provenance",
        default="synthetic_controlled_annotation",
        choices=("synthetic_controlled_annotation", "human_review"),
    )
    annotate.add_argument(
        "--dimension", action="append", help="key=good|acceptable|poor, repeatable"
    )
    annotate.add_argument("--unusable", action="store_true")
    annotate.set_defaults(func=_cmd_annotate)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("case_id")
    resolve.add_argument("--resolver", required=True)
    resolve.add_argument("--observed-mode", required=True, choices=_OBSERVED_MODES)
    resolve.add_argument("--rationale", required=True)
    resolve.add_argument(
        "--provenance",
        default="synthetic_controlled_annotation",
        choices=("synthetic_controlled_annotation", "human_review"),
    )
    resolve.set_defaults(func=_cmd_resolve)

    subparsers.add_parser("status").set_defaults(func=_cmd_status)
    subparsers.add_parser("agreement").set_defaults(func=_cmd_agreement)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
