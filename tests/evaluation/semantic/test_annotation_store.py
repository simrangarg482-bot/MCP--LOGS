"""Tests for `app.evaluation.semantic.annotation_store` -- Priority 11's
append-only ground-truth persistence layer. Section 20's "Annotation
contract" and "Independent review" requirements.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.evaluation.semantic import annotation_store
from app.evaluation.semantic.schemas import AnnotationDecision, ResolutionAnnotation


def _annotation(**overrides) -> AnnotationDecision:
    payload = {
        "case_id": "case-1",
        "dataset_version": "test-v1",
        "case_snapshot_hash": "abc123",
        "reviewer_id": "reviewer-a",
        "provenance": "synthetic_controlled_annotation",
        "annotated_at": datetime.now(UTC),
        "observed_mode": "no_answer",
        "rationale": "evidence has no bearing on the question",
    }
    payload.update(overrides)
    return AnnotationDecision(**payload)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(annotation_store, "_ANNOTATIONS_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# annotation contract
# --------------------------------------------------------------------------


def test_invalid_observed_mode_is_rejected():
    with pytest.raises(ValidationError):
        _annotation(observed_mode="maybe")


def test_missing_rationale_is_rejected():
    with pytest.raises(ValidationError):
        AnnotationDecision(
            case_id="c",
            dataset_version="v",
            case_snapshot_hash="h",
            reviewer_id="r",
            provenance="synthetic_controlled_annotation",
            annotated_at=datetime.now(UTC),
            observed_mode="no_answer",
        )  # type: ignore[call-arg]


def test_invalid_provenance_is_rejected():
    with pytest.raises(ValidationError):
        _annotation(provenance="totally_trustworthy")


def test_dimension_rating_out_of_vocabulary_is_rejected():
    with pytest.raises(ValidationError):
        _annotation(dimension_ratings={"correctness": "excellent"})


def test_schema_version_defaults_and_is_recorded():
    annotation = _annotation()
    assert annotation.annotation_schema_version == "annotation-v1"


def test_dataset_version_is_recorded_on_the_annotation():
    annotation = _annotation(dataset_version="semantic-v7")
    assert annotation.dataset_version == "semantic-v7"


# --------------------------------------------------------------------------
# evidence snapshot hashing
# --------------------------------------------------------------------------


def test_snapshot_hash_is_stable_regardless_of_evidence_order():
    a = annotation_store.compute_case_snapshot_hash("q", ["one", "two"], "answer")
    b = annotation_store.compute_case_snapshot_hash("q", ["two", "one"], "answer")
    assert a == b


def test_snapshot_hash_changes_with_different_answer_text():
    a = annotation_store.compute_case_snapshot_hash("q", ["one"], "answer A")
    b = annotation_store.compute_case_snapshot_hash("q", ["one"], "answer B")
    assert a != b


# --------------------------------------------------------------------------
# append-only store: independent review, duplicates
# --------------------------------------------------------------------------


def test_first_annotation_is_accepted_and_loadable():
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a"))
    loaded = annotation_store.load_annotations("test-v1")
    assert len(loaded) == 1
    assert loaded[0].reviewer_id == "reviewer-a"


def test_second_independent_annotation_is_accepted():
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a"))
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-b"))
    loaded = annotation_store.load_annotations("test-v1")
    assert {a.reviewer_id for a in loaded} == {"reviewer-a", "reviewer-b"}


def test_duplicate_reviewer_submission_for_the_same_case_is_rejected():
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a"))
    with pytest.raises(annotation_store.DuplicateAnnotationError):
        annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a"))
    # The original is untouched -- still exactly one record.
    assert len(annotation_store.load_annotations("test-v1")) == 1


def test_same_reviewer_can_annotate_a_different_case():
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a", case_id="case-1"))
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a", case_id="case-2"))
    assert len(annotation_store.load_annotations("test-v1")) == 2


def test_annotations_for_missing_dataset_version_return_empty_not_error():
    assert annotation_store.load_annotations("never-seen") == []
    assert annotation_store.load_resolutions("never-seen") == []


def test_resolution_is_appended_and_does_not_touch_original_annotations():
    annotation_store.save_annotation(
        _annotation(reviewer_id="reviewer-a", observed_mode="no_answer")
    )
    annotation_store.save_annotation(
        _annotation(reviewer_id="reviewer-b", observed_mode="substantive_answer")
    )
    resolution = ResolutionAnnotation(
        case_id="case-1",
        dataset_version="test-v1",
        case_snapshot_hash="abc123",
        reviewer_id="resolver-1",
        provenance="synthetic_controlled_annotation",
        annotated_at=datetime.now(UTC),
        observed_mode="no_answer",
        rationale="siding with reviewer-a: the text is a clear decline",
        resolved_annotation_ids=["reviewer-a:x", "reviewer-b:y"],
    )
    annotation_store.save_resolution(resolution)

    originals = annotation_store.load_annotations("test-v1")
    assert len(originals) == 2  # both originals still present, unmodified
    assert {a.observed_mode for a in originals} == {"no_answer", "substantive_answer"}
    resolutions = annotation_store.load_resolutions("test-v1")
    assert len(resolutions) == 1
    assert resolutions[0].observed_mode == "no_answer"


def test_store_is_append_only_across_process_boundaries(tmp_path, monkeypatch):
    """Simulates a second, later run reading a store a prior run wrote --
    section 6's "a previously generated report remains interpretable"
    guarantee, checked at the storage layer."""
    monkeypatch.setattr(annotation_store, "_ANNOTATIONS_DIR", tmp_path)
    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-a"))
    first_read = annotation_store.load_annotations("test-v1")

    annotation_store.save_annotation(_annotation(reviewer_id="reviewer-b"))
    second_read = annotation_store.load_annotations("test-v1")

    assert len(first_read) == 1
    assert len(second_read) == 2
    assert first_read[0].reviewer_id in {a.reviewer_id for a in second_read}
