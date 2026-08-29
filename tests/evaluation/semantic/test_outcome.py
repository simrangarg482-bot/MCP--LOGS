"""Tests for `app.evaluation.semantic.outcome` -- the deterministic
answer-mode-comparison layer Priority 9 adds ahead of the semantic rubric.
Covers section 14's explicit "deterministic mode correctness" combinations
and the refusal-detection reuse of production sentinels.
"""

from __future__ import annotations

from app.agents.answer.generation import _NO_ANSWER_MARKER
from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE
from app.evaluation.semantic.outcome import classify_outcome_correctness, is_refusal_text

# --------------------------------------------------------------------------
# is_refusal_text -- reuses production sentinels, never a text heuristic
# --------------------------------------------------------------------------


def test_no_answer_marker_is_a_refusal():
    assert is_refusal_text(_NO_ANSWER_MARKER) is True


def test_insufficient_grounding_message_is_a_refusal():
    assert is_refusal_text(_INSUFFICIENT_GROUNDING_MESSAGE) is True


def test_substantive_answer_is_not_a_refusal():
    assert is_refusal_text("The service went down due to connection pool exhaustion.") is False


def test_an_answer_that_merely_mentions_uncertainty_is_not_a_refusal():
    """Hedging is NOT the same as refusing -- that distinction is the
    semantic judge's job (`observed_mode`), not this deterministic check's.
    A substantive answer that happens to contain uncertainty language must
    not be misclassified as a refusal by this function."""
    assert (
        is_refusal_text(
            "Based on one source it appears the timeout was reduced, though I can't "
            "fully confirm this without more context."
        )
        is False
    )


def test_whitespace_padded_insufficient_grounding_message_is_still_a_refusal():
    assert is_refusal_text(f"  {_INSUFFICIENT_GROUNDING_MESSAGE}  ") is True


# --------------------------------------------------------------------------
# classify_outcome_correctness -- pure lookup, section 14's required combos
# --------------------------------------------------------------------------


def test_unlabeled_expected_mode_is_excluded_not_guessed():
    assert classify_outcome_correctness("unlabeled", "substantive_answer") is None
    assert classify_outcome_correctness("unlabeled", "no_answer") is None
    assert classify_outcome_correctness("unlabeled", "qualified_answer") is None


def test_expected_answer_observed_substantive_answer_is_correct():
    assert classify_outcome_correctness("answer", "substantive_answer") == "correct"


def test_expected_answer_observed_refusal_is_incorrect_refusal():
    assert classify_outcome_correctness("answer", "no_answer") == "incorrect_refusal"


def test_expected_answer_observed_qualified_is_partially_correct():
    assert classify_outcome_correctness("answer", "qualified_answer") == "partially_correct"


def test_expected_no_answer_observed_no_answer_is_correct():
    assert classify_outcome_correctness("no_answer", "no_answer") == "correct"


def test_expected_no_answer_observed_substantive_is_critical_failure():
    assert classify_outcome_correctness("no_answer", "substantive_answer") == "critical_failure"


def test_expected_no_answer_observed_qualified_is_also_critical_failure():
    """Even a hedged answer invents something when the case declares the
    evidence has no real bearing on the question at all -- there is no
    partial credit for hedging about nothing."""
    assert classify_outcome_correctness("no_answer", "qualified_answer") == "critical_failure"


def test_expected_qualified_observed_qualified_is_correct():
    assert classify_outcome_correctness("qualified_answer", "qualified_answer") == "correct"


def test_expected_qualified_observed_overconfident_substantive_is_overconfident():
    assert classify_outcome_correctness("qualified_answer", "substantive_answer") == "overconfident"


def test_expected_qualified_observed_no_answer_is_correct_not_incorrect_refusal():
    """A cautious decline on merely-partial evidence is treated as a good
    outcome, matching this codebase's own established philosophy
    (`agents.answer.sufficiency`'s docstring, `tests/rag_validation`'s "a
    confidently wrong answer is the single most damaging failure mode") --
    NOT penalized as an incorrect refusal."""
    assert classify_outcome_correctness("qualified_answer", "no_answer") == "correct"


def test_every_non_unlabeled_expected_mode_paired_with_every_observed_mode_is_covered():
    """No combination silently falls through to a KeyError at runtime --
    the lookup table is exhaustive for every reachable (expected, observed)
    pair."""
    expected_modes = ["answer", "qualified_answer", "no_answer"]
    observed_modes = ["substantive_answer", "qualified_answer", "no_answer"]
    for expected in expected_modes:
        for observed in observed_modes:
            assert classify_outcome_correctness(expected, observed) is not None
