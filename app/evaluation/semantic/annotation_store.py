"""Append-only storage for `AnnotationDecision`/`ResolutionAnnotation`
records -- Priority 11's ground-truth layer.

WHY A JSON FIXTURE FILE, NOT A DATABASE TABLE
    Every existing piece of this codebase's evaluation infrastructure
    (`app/evaluation/fixtures/*.jsonl`, `scripts/eval_confidence_dataset.
    json`, `app/evaluation/semantic/fixtures.py`) is version-controlled,
    file-based, and reviewed the same way code is -- there is no precedent
    anywhere in `app/evaluation/` for a database table backing benchmark
    data, and Tier 1/Tier 3 both run as standalone scripts, never as a
    request-scoped REST resource. Ground-truth annotations are the same
    kind of thing: a golden dataset a team curates over time, not
    per-organization customer data -- so this reuses that exact
    convention (one JSON file per dataset version, loaded/appended by a
    small Python module) rather than adding a new migration, a new table,
    and a new authorization surface for what is fundamentally the same
    "reviewed fixture" pattern this package already has three examples of.
    Section 5's tenant/reviewer-authorization questions are consequently
    inapplicable by construction: there is no per-organization row to leak
    across a tenant boundary, because there is no organization scoping
    here at all -- see `docs/SEMANTIC_BENCHMARK.md`'s "Why not
    database-backed" for the full reasoning, including what WOULD justify
    revisiting this if a real multi-reviewer team needed concurrent write
    access this file-based approach doesn't support well.

APPEND-ONLY, NEVER OVERWRITE (section 6)
    `save_annotation` refuses to write a record whose `(reviewer_id,
    case_id, dataset_version)` already exists in the store -- a reviewer
    correcting their own mistake submits a NEW annotation (a later
    `annotated_at` naturally supersedes an earlier one for agreement
    purposes is a `ground_truth.py` concern, not this module's), or a
    resolver submits a `ResolutionAnnotation`; nothing here ever mutates or
    deletes a prior record. This is what makes section 6's "a previously
    generated evaluation report remains interpretable against the exact
    ground truth available when that report was produced" true by
    construction: the file this priority's tooling reads from only ever
    grows.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.evaluation.semantic.schemas import (
    AnnotationDecision,
    AnswerQualityCase,
    ResolutionAnnotation,
)

#: One file per dataset version -- mirrors `fixtures.DATASET_VERSION`'s own
#: versioning granularity, so an annotation file is never ambiguous about
#: which case set it was written against.
_ANNOTATIONS_DIR = Path(__file__).resolve().parent / "annotations"


class DuplicateAnnotationError(Exception):
    """Raised when a reviewer submits a second annotation for a
    (case_id, dataset_version) pair they already annotated -- a reviewer
    correcting themselves should not silently overwrite their own prior
    judgment (that would defeat the whole "immutable history" point);
    callers that genuinely want to replace a submission should say so
    explicitly by first inspecting `load_annotations` themselves, not by
    this module silently allowing it."""


def compute_case_snapshot_hash(
    question: str, evidence_texts: list[str], candidate_answer: str
) -> str:
    """A stable fingerprint of exactly what a reviewer was shown -- section
    12's evidence-snapshot requirement. `evidence_texts` is sorted before
    hashing so two calls with the same evidence in a different order
    produce the same hash (list construction order is an implementation
    detail, not part of what was actually shown). Not cryptographically
    sensitive -- SHA-256 chosen only for a low collision rate on plain
    text, not for any security property.
    """
    payload = json.dumps(
        {"question": question, "evidence": sorted(evidence_texts), "answer": candidate_answer},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def case_snapshot_hash_for(case: AnswerQualityCase, candidate_answer: str) -> str:
    return compute_case_snapshot_hash(case.question, case.evidence_texts, candidate_answer)


def _store_path(dataset_version: str) -> Path:
    safe_name = dataset_version.replace("/", "_")
    return _ANNOTATIONS_DIR / f"{safe_name}.annotations.json"


def load_annotations(dataset_version: str) -> list[AnnotationDecision]:
    """Returns `[]` (not an error) if no annotation file exists yet for
    this dataset version -- the same honest-empty convention `fixtures.
    load_repository_derived_answer_quality_cases` already uses."""
    path = _store_path(dataset_version)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [AnnotationDecision.model_validate(entry) for entry in raw.get("annotations", [])]


def load_resolutions(dataset_version: str) -> list[ResolutionAnnotation]:
    path = _store_path(dataset_version)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ResolutionAnnotation.model_validate(entry) for entry in raw.get("resolutions", [])]


def _read_raw(dataset_version: str) -> dict:
    path = _store_path(dataset_version)
    if not path.exists():
        return {"annotations": [], "resolutions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw(dataset_version: str, raw: dict) -> None:
    _ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _store_path(dataset_version)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True, default=str), encoding="utf-8")


def save_annotation(annotation: AnnotationDecision) -> None:
    """Appends `annotation` to its dataset version's store. Raises
    `DuplicateAnnotationError` if this exact `(reviewer_id, case_id)` pair
    has already submitted an annotation for this dataset version -- see
    module docstring.
    """
    raw = _read_raw(annotation.dataset_version)
    for existing in raw["annotations"]:
        if existing["reviewer_id"] == annotation.reviewer_id and existing["case_id"] == (
            annotation.case_id
        ):
            raise DuplicateAnnotationError(
                f"reviewer {annotation.reviewer_id!r} already annotated case "
                f"{annotation.case_id!r} in dataset {annotation.dataset_version!r} at "
                f"{existing['annotated_at']!r} -- annotations are append-only and never "
                "overwritten; submit under a different reviewer_id, or use the resolution "
                "workflow if this is meant to supersede a disagreement"
            )
    raw["annotations"].append(json.loads(annotation.model_dump_json()))
    _write_raw(annotation.dataset_version, raw)


def save_resolution(resolution: ResolutionAnnotation) -> None:
    """Appends a disagreement resolution -- never modifies the original
    annotations it resolves (`resolution.resolved_annotation_ids` records
    which ones, but their own stored records are untouched)."""
    raw = _read_raw(resolution.dataset_version)
    raw.setdefault("resolutions", []).append(json.loads(resolution.model_dump_json()))
    _write_raw(resolution.dataset_version, raw)
