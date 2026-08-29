"""Tests for `app.evaluation.adapters.eval_confidence_report` -- derives
calibration pairs from `scripts/eval_confidence.py`'s own report shape.
"""

from __future__ import annotations

import json

from app.evaluation.adapters.eval_confidence_report import load_calibration_pairs


def _question(
    category: str, confidence: float | None, is_real_answer: bool, error: str | None = None
) -> dict:
    return {
        "category": category,
        "confidence_score": confidence,
        "is_real_answer": is_real_answer,
        "error": error,
    }


def _write_report(tmp_path, questions: list[dict]):
    path = tmp_path / "report.json"
    payload = {"generated_at": "2026-08-01T00:00:00Z", "questions": questions}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_clear_answer_correctly_answered_is_correct(tmp_path):
    path = _write_report(tmp_path, [_question("clear-answer", 0.8, True)])
    assert load_calibration_pairs(path) == [(0.8, True)]


def test_ambiguous_confidently_answered_is_incorrect(tmp_path):
    path = _write_report(tmp_path, [_question("ambiguous", 0.75, True)])
    assert load_calibration_pairs(path) == [(0.75, False)]


def test_no_information_correctly_declined_is_correct(tmp_path):
    path = _write_report(tmp_path, [_question("no-information", 0.3, False)])
    assert load_calibration_pairs(path) == [(0.3, True)]


def test_errored_questions_are_excluded(tmp_path):
    path = _write_report(tmp_path, [_question("clear-answer", 0.8, True, error="network blip")])
    assert load_calibration_pairs(path) == []


def test_missing_confidence_score_excluded(tmp_path):
    path = _write_report(tmp_path, [_question("clear-answer", None, True)])
    assert load_calibration_pairs(path) == []


def test_empty_questions_list_returns_empty_pairs(tmp_path):
    path = _write_report(tmp_path, [])
    assert load_calibration_pairs(path) == []
