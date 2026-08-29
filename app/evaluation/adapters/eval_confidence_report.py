"""Reads `scripts/eval_confidence.py`'s own JSON report and derives
`(predicted_confidence, was_correct)` pairs for
`app.evaluation.metrics.confidence.compute_calibration` -- real production
measurements, feeding this package's calibration analyzer without this
package ever running the live pipeline itself.

This is the one place in `app.evaluation` that reaches outside its own
fixtures for calibration input, specifically because `eval_confidence.py`
already IS the real, live, funded-`OPENAI_API_KEY` measurement this
package's Mode 3 would otherwise have to duplicate -- see this package's
`__init__.py` docstring.

Ground-truth mapping mirrors (does not import -- `scripts/` is not an
importable package, and this is a two-line judgment, not pipeline logic)
`scripts/eval_confidence.py`'s own `QuestionResult.is_positive`/
`is_real_answer` distinction: a question is "positive" (should have been
answered) only in the `clear-answer` category; "correct" means the routing
decision's actual outcome (`is_real_answer`) matched that ground truth
exactly, in either direction (a `clear-answer` question that WAS answered
is correct; an `ambiguous`/`no-information` question that was NOT answered
is also correct).
"""

from __future__ import annotations

import json
from pathlib import Path

#: Mirrors `scripts/eval_confidence.py`'s `_POSITIVE_CATEGORIES` -- see
#: module docstring for why this is duplicated, not imported.
_POSITIVE_CATEGORIES = frozenset({"clear-answer"})


def load_calibration_pairs(report_path: Path) -> list[tuple[float, bool]]:
    """Parse one `eval_confidence_report.json`-shaped file into calibration
    pairs. Skips any question with a recorded `error` (a harness/network
    failure, not evidence about calibration one way or the other -- same
    exclusion `eval_confidence.py`'s own `_confusion_at` already applies)
    and any with a `None` confidence score.
    """
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    pairs: list[tuple[float, bool]] = []
    for question in payload.get("questions", []):
        if question.get("error") is not None:
            continue
        confidence = question.get("confidence_score")
        if confidence is None:
            continue
        is_positive = question.get("category") in _POSITIVE_CATEGORIES
        is_real_answer = bool(question.get("is_real_answer"))
        correct = is_real_answer == is_positive
        pairs.append((float(confidence), correct))
    return pairs
