"""Dataset-loading tests for `app.evaluation.semantic.fixtures` -- section
14's "unlabeled cases handled explicitly" and the contrast-fixture
structure Priority 9 section 6 requires (paired same-question/same-
evidence cases with a deliberately different expected evaluation).
"""

from __future__ import annotations

import json

from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE
from app.evaluation.semantic.fixtures import (
    CONTRAST_ANSWER_QUALITY_CASES,
    SYNTHETIC_ANSWER_QUALITY_CASES,
    SYNTHETIC_INVESTIGATION_AB_CASES,
    load_repository_derived_answer_quality_cases,
)


def test_every_synthetic_answer_quality_case_declares_a_non_unlabeled_mode():
    """The hand-authored corpus always knows what it's testing -- only
    repository-derived cases may legitimately fall back to unlabeled."""
    for case in SYNTHETIC_ANSWER_QUALITY_CASES:
        assert case.expected_answer_mode != "unlabeled", case.id


def test_every_contrast_case_declares_a_non_unlabeled_mode():
    for case in CONTRAST_ANSWER_QUALITY_CASES:
        assert case.expected_answer_mode != "unlabeled", case.id


def test_contrast_cases_are_three_same_question_same_evidence_pairs():
    by_id = {c.id: c for c in CONTRAST_ANSWER_QUALITY_CASES}
    pairs = [
        ("contrast-a-correct-refusal", "contrast-c-hallucination"),
        ("contrast-b-correct-answer", "contrast-d-incorrect-refusal"),
        ("contrast-e-qualified", "contrast-f-overconfident"),
    ]
    for left_id, right_id in pairs:
        left, right = by_id[left_id], by_id[right_id]
        assert left.question == right.question, f"{left_id}/{right_id} question mismatch"
        assert left.evidence_texts == right.evidence_texts, (
            f"{left_id}/{right_id} evidence mismatch"
        )
        assert left.fixed_answer != right.fixed_answer, f"{left_id}/{right_id} answers must differ"


def test_refusal_labelled_contrast_cases_use_a_real_detectable_refusal_sentinel():
    """A contrast case whose fixed_answer is meant to be detected as a
    refusal must actually BE detected as one by `outcome.is_refusal_text`
    -- otherwise it silently tests the wrong rubric path. Regression guard
    for the exact bug this priority's own test suite caught during
    development: hand-authored refusal prose that doesn't match either
    production sentinel routes to the substantive rubric instead."""
    from app.evaluation.semantic.outcome import is_refusal_text

    for case_id in ("contrast-a-correct-refusal", "contrast-d-incorrect-refusal"):
        case = next(c for c in CONTRAST_ANSWER_QUALITY_CASES if c.id == case_id)
        assert is_refusal_text(case.fixed_answer), case_id


def test_hallucination_and_overconfident_contrast_cases_are_not_detected_as_refusals():
    for case_id in ("contrast-c-hallucination", "contrast-f-overconfident"):
        from app.evaluation.semantic.outcome import is_refusal_text

        case = next(c for c in CONTRAST_ANSWER_QUALITY_CASES if c.id == case_id)
        assert not is_refusal_text(case.fixed_answer), case_id


def test_contrast_correct_refusal_case_uses_the_real_production_sentinel():
    """Not an invented refusal phrase -- the exact string the real Answer
    Agent node emits (`agents.answer.node._INSUFFICIENT_GROUNDING_MESSAGE`),
    reused verbatim."""
    case = next(c for c in CONTRAST_ANSWER_QUALITY_CASES if c.id == "contrast-a-correct-refusal")
    assert case.fixed_answer == _INSUFFICIENT_GROUNDING_MESSAGE


def test_investigation_ab_cases_still_construct_unchanged():
    assert len(SYNTHETIC_INVESTIGATION_AB_CASES) == 3


def test_repository_derived_loader_returns_empty_when_dataset_file_missing(monkeypatch):
    from pathlib import Path

    import app.evaluation.semantic.fixtures as fixtures_module

    monkeypatch.setattr(
        fixtures_module, "_EVAL_CONFIDENCE_DATASET_PATH", Path("/does/not/exist.json")
    )
    assert load_repository_derived_answer_quality_cases() == []


def test_repository_derived_loader_labels_each_category_with_its_expected_mode(
    tmp_path, monkeypatch
):
    import app.evaluation.semantic.fixtures as fixtures_module

    dataset = {
        "questions": [
            {"id": "q1", "category": "clear-answer", "question": "Q1"},
            {"id": "q2", "category": "ambiguous", "question": "Q2"},
            {"id": "q3", "category": "no-information", "question": "Q3"},
        ]
    }
    path = tmp_path / "eval_confidence_dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(fixtures_module, "_EVAL_CONFIDENCE_DATASET_PATH", path)

    cases = load_repository_derived_answer_quality_cases()
    modes = {c.id: c.expected_answer_mode for c in cases}
    assert modes["repo-q1"] == "answer"
    assert modes["repo-q2"] == "qualified_answer"
    assert modes["repo-q3"] == "no_answer"


def test_repository_derived_loader_labels_unrecognized_category_as_unlabeled_not_guessed(
    tmp_path, monkeypatch
):
    import app.evaluation.semantic.fixtures as fixtures_module

    dataset = {"questions": [{"id": "q1", "category": "some-future-category", "question": "Q1"}]}
    path = tmp_path / "eval_confidence_dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(fixtures_module, "_EVAL_CONFIDENCE_DATASET_PATH", path)

    cases = load_repository_derived_answer_quality_cases()
    assert len(cases) == 1
    assert cases[0].expected_answer_mode == "unlabeled"


def test_repository_derived_loader_limit_applies_per_category(tmp_path, monkeypatch):
    import app.evaluation.semantic.fixtures as fixtures_module

    dataset = {
        "questions": [
            {"id": "a1", "category": "clear-answer", "question": "A1"},
            {"id": "a2", "category": "clear-answer", "question": "A2"},
            {"id": "n1", "category": "no-information", "question": "N1"},
            {"id": "n2", "category": "no-information", "question": "N2"},
        ]
    }
    path = tmp_path / "eval_confidence_dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(fixtures_module, "_EVAL_CONFIDENCE_DATASET_PATH", path)

    cases = load_repository_derived_answer_quality_cases(limit=1)
    # 1 from clear-answer + 1 from no-information (ambiguous category has no rows here)
    assert len(cases) == 2
    assert {c.expected_answer_mode for c in cases} == {"answer", "no_answer"}


# --------------------------------------------------------------------------
# human-annotatable corpus (Priority 11)
# --------------------------------------------------------------------------


def test_annotatable_corpus_every_case_has_a_fixed_answer():
    from app.evaluation.semantic.fixtures import load_annotatable_answer_quality_cases

    for case in load_annotatable_answer_quality_cases():
        assert case.fixed_answer is not None, case.id


def test_annotatable_corpus_includes_the_six_contrast_cases_and_three_repo_derived():
    from app.evaluation.semantic.fixtures import load_annotatable_answer_quality_cases

    cases = load_annotatable_answer_quality_cases()
    ids = {c.id for c in cases}
    assert {c.id for c in CONTRAST_ANSWER_QUALITY_CASES} <= ids
    repo_derived = [c for c in cases if c.provenance == "repository_derived"]
    assert len(repo_derived) == 3


def test_annotatable_corpus_spans_all_three_expected_modes():
    from app.evaluation.semantic.fixtures import load_annotatable_answer_quality_cases

    modes = {c.expected_answer_mode for c in load_annotatable_answer_quality_cases()}
    assert modes == {"answer", "qualified_answer", "no_answer"}
