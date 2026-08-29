"""Deterministic answer-mode detection + outcome-correctness classification
-- Priority 9's fix for the scoring bug Priority 8 shipped and then caught
in its own live run: a correct `NO_ANSWER` refusal scored 0.0 on every
answer-quality dimension because the rubric assumed every good answer is
substantive.

THE PIPELINE THIS MODULE IS THE FIRST TWO STAGES OF (section 7)
    case metadata -> **deterministic answer-mode comparison** (this module)
    -> mode-specific semantic rubric (`answer_quality.py`) -> dimension
    scores -> aggregate outcome (`runner.py`).

WHY REFUSAL DETECTION IS DETERMINISTIC, NOT AN LLM CALL
    `is_refusal_text` reuses the exact two textual signals the production
    Answer Agent already emits for "I am not answering this" -- the model's
    own `NO_ANSWER` marker (`agents.answer.generation.is_no_answer`, checked
    before the marker is ever stripped) and the Answer Agent node's fixed
    fallback sentence (`agents.answer.node._INSUFFICIENT_GROUNDING_MESSAGE`,
    reached after `call_with_retry`'s budget is exhausted). Both are exact,
    known strings this codebase already controls -- there is nothing semantic
    to judge about whether a string equals a known sentinel, so spending an
    LLM call on it would be exactly the "unnecessary LLM call" section 7
    warns against. Distinguishing "substantive" from "qualified" (hedged) IS
    inherently semantic -- see `answer_quality.py`'s single mode-aware judge
    call for where that classification actually happens.

WHY OUTCOME-CORRECTNESS IS A PURE LOOKUP, NOT MODEL OUTPUT
    `classify_outcome_correctness` never asks an LLM to grade whether a mode
    match is "correct" -- the case's `expected_answer_mode` is a ground-truth
    declaration made independently by the corpus author (section 5: the
    benchmark must never let a model's own behavior stand in for what was
    expected), and the observed mode is either detected deterministically
    (refusal) or classified by a judge call that never sees
    `expected_answer_mode` in the first place (see `answer_quality.py`) --
    so comparing the two here can be, and is, plain code.
"""

from __future__ import annotations

from app.agents.answer.generation import is_no_answer
from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE
from app.evaluation.semantic.schemas import (
    AnswerOutcomeCorrectness,
    ExpectedAnswerMode,
    ObservedAnswerMode,
)


def is_refusal_text(text: str) -> bool:
    """Whether `text` is a refusal by this codebase's own two known
    sentinels -- never a heuristic guess at "sounds like a refusal", which
    would risk misclassifying a substantive answer that merely mentions
    uncertainty (that case is exactly what `"qualified_answer"` exists to
    distinguish, via the semantic judge call, not this function).
    """
    return is_no_answer(text) or text.strip() == _INSUFFICIENT_GROUNDING_MESSAGE


#: `(expected, observed) -> outcome`. See module docstring and
#: `AnswerOutcomeCorrectness`'s own docstring in `schemas.py` for the
#: reasoning behind each cell; the two decisions most worth restating here:
#:
#: `("qualified_answer", "no_answer") -> "correct"`: declining is treated as
#: a GOOD outcome when the case's own evidence is merely partial, not a
#: worse outcome than hedging -- matching this codebase's own established
#: philosophy (`agents.answer.sufficiency`'s docstring, `tests/
#: rag_validation/README.md`'s "a confidently wrong answer is the single
#: most damaging failure mode"): a cautious decline on ambiguous evidence is
#: what `assess_sufficiency` was built to produce more of, not a defect to
#: penalize here.
#:
#: `("no_answer", "qualified_answer") -> "critical_failure"`, same as
#: `("no_answer", "substantive_answer")`: when the case declares the
#: evidence has NO real bearing on the question at all, even a hedged
#: answer is inventing something from nothing -- there is no "partial
#: credit for hedging" when there was never anything to hedge about.
_OUTCOME_TABLE: dict[tuple[ExpectedAnswerMode, ObservedAnswerMode], AnswerOutcomeCorrectness] = {
    ("answer", "substantive_answer"): "correct",
    ("answer", "qualified_answer"): "partially_correct",
    ("answer", "no_answer"): "incorrect_refusal",
    ("qualified_answer", "qualified_answer"): "correct",
    ("qualified_answer", "substantive_answer"): "overconfident",
    ("qualified_answer", "no_answer"): "correct",
    ("no_answer", "no_answer"): "correct",
    ("no_answer", "substantive_answer"): "critical_failure",
    ("no_answer", "qualified_answer"): "critical_failure",
}


def classify_outcome_correctness(
    expected: ExpectedAnswerMode, observed: ObservedAnswerMode
) -> AnswerOutcomeCorrectness | None:
    """`None` for `expected == "unlabeled"` -- excluded from outcome-
    correctness metrics rather than guessed (section 5), the same
    honest-exclusion convention `calibration.py`'s sample-size floor uses
    for "not enough data to conclude" instead of fabricating a conclusion.
    """
    if expected == "unlabeled":
        return None
    return _OUTCOME_TABLE[(expected, observed)]
