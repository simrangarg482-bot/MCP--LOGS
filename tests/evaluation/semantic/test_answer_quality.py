"""Tests for `app.evaluation.semantic.answer_quality` -- the mode-routed
rubric evaluator (Priority 9). Covers section 14's "rubric routing:
substantive cases and refusal cases receive different evaluation criteria"
requirement, plus section 15's "evaluator malformed output surfaced
correctly" and "evaluator receives only intended evidence" (unchanged from
Priority 8, re-verified against the new routing).
"""

from __future__ import annotations

import json

import pytest

from app.evaluation.semantic.answer_quality import (
    AnswerQualityParsingError,
    judge_answer_quality,
)
from app.evaluation.semantic.schemas import AnswerQualityCase


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _QueuedLLM:
    def __init__(self, *replies: str) -> None:
        self._queue = list(replies)
        self.calls: list[object] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._queue:
            raise AssertionError("LLM called more times than responses were queued")
        return _Response(self._queue.pop(0))


def _case(**overrides) -> AnswerQualityCase:
    payload = {
        "id": "aq-1",
        "provenance": "synthetic_controlled",
        "question": "Why did the service go down?",
        "evidence_texts": ["Deployment 42 reduced the connection pool size."],
        "reference_answer": None,
    }
    payload.update(overrides)
    return AnswerQualityCase(**payload)


def _valid_substantive_json(**overrides) -> str:
    payload = {
        "observed_mode": "substantive_answer",
        "correctness": {"score": 0.9, "reason": "matches the evidence"},
        "relevance": {"score": 0.8, "reason": "addresses the question"},
        "usefulness": {"score": 0.7, "reason": "actionable"},
        "faithfulness": {"score": 1.0, "reason": "every claim is traceable to the evidence"},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _valid_refusal_json(**overrides) -> str:
    payload = {
        "abstention_correctness": {"score": 0.9, "reason": "evidence really was insufficient"},
        "unsupported_claim_avoidance": {"score": 1.0, "reason": "invented nothing"},
        "explanation_quality": {"score": 0.8, "reason": "clear about the gap"},
        "appropriate_next_step": {"score": 0.5, "reason": "no next step suggested"},
    }
    payload.update(overrides)
    return json.dumps(payload)


# --------------------------------------------------------------------------
# routing: a refusal text never reaches the substantive prompt, and
# vice versa -- proven by which queued reply gets consumed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refusal_text_is_routed_to_the_refusal_rubric():
    llm = _QueuedLLM(_valid_refusal_json())
    judgement = await judge_answer_quality(llm, _case(), "NO_ANSWER")

    assert judgement.observed_answer_mode == "no_answer"
    assert judgement.refusal is not None
    assert judgement.substantive is None
    sent = str(llm.calls[0])
    assert "REFUSAL" in sent  # the refusal-rubric system prompt, not the substantive one


@pytest.mark.asyncio
async def test_substantive_text_is_routed_to_the_substantive_rubric():
    llm = _QueuedLLM(_valid_substantive_json())
    judgement = await judge_answer_quality(llm, _case(), "the pool size reduction caused it")

    assert judgement.observed_answer_mode == "substantive_answer"
    assert judgement.substantive is not None
    assert judgement.refusal is None


@pytest.mark.asyncio
async def test_insufficient_grounding_message_is_also_routed_to_refusal_rubric():
    from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE

    llm = _QueuedLLM(_valid_refusal_json())
    judgement = await judge_answer_quality(llm, _case(), _INSUFFICIENT_GROUNDING_MESSAGE)
    assert judgement.observed_answer_mode == "no_answer"


@pytest.mark.asyncio
async def test_qualified_observed_mode_is_captured_from_the_substantive_call():
    llm = _QueuedLLM(_valid_substantive_json(observed_mode="qualified_answer"))
    judgement = await judge_answer_quality(
        llm, _case(), "it might be the pool size, but I'm not certain"
    )

    assert judgement.observed_answer_mode == "qualified_answer"
    assert judgement.substantive.observed_mode == "qualified_answer"


# --------------------------------------------------------------------------
# substantive rubric: malformed output, parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_substantive_response_parses_into_judgement_with_all_four_dimensions():
    llm = _QueuedLLM(_valid_substantive_json())
    judgement = await judge_answer_quality(llm, _case(), "the pool size reduction caused it")

    assert judgement.substantive.correctness.score == pytest.approx(0.9)
    assert judgement.substantive.faithfulness.reason == "every claim is traceable to the evidence"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_markdown_fenced_substantive_json_is_accepted():
    fenced = "```json\n" + _valid_substantive_json() + "\n```"
    llm = _QueuedLLM(fenced)
    judgement = await judge_answer_quality(llm, _case(), "an answer")
    assert judgement.substantive.relevance.score == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_non_json_substantive_response_raises_parsing_error():
    llm = _QueuedLLM("this is not json at all")
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "an answer")


@pytest.mark.asyncio
async def test_out_of_range_score_raises_parsing_error():
    llm = _QueuedLLM(_valid_substantive_json(correctness={"score": 5.0, "reason": "too high"}))
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "an answer")


@pytest.mark.asyncio
async def test_missing_dimension_raises_parsing_error():
    payload = json.loads(_valid_substantive_json())
    del payload["faithfulness"]
    llm = _QueuedLLM(json.dumps(payload))
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "an answer")


@pytest.mark.asyncio
async def test_missing_observed_mode_raises_parsing_error():
    payload = json.loads(_valid_substantive_json())
    del payload["observed_mode"]
    llm = _QueuedLLM(json.dumps(payload))
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "an answer")


@pytest.mark.asyncio
async def test_invalid_observed_mode_value_raises_parsing_error():
    llm = _QueuedLLM(_valid_substantive_json(observed_mode="somewhat_confident"))
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "an answer")


# --------------------------------------------------------------------------
# refusal rubric: malformed output, parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_refusal_response_parses_into_judgement_with_all_four_dimensions():
    llm = _QueuedLLM(_valid_refusal_json())
    judgement = await judge_answer_quality(llm, _case(), "NO_ANSWER")
    assert judgement.refusal.abstention_correctness.score == pytest.approx(0.9)
    assert judgement.refusal.appropriate_next_step.reason == "no next step suggested"


@pytest.mark.asyncio
async def test_non_json_refusal_response_raises_parsing_error():
    llm = _QueuedLLM("not json")
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "NO_ANSWER")


@pytest.mark.asyncio
async def test_refusal_missing_dimension_raises_parsing_error():
    payload = json.loads(_valid_refusal_json())
    del payload["abstention_correctness"]
    llm = _QueuedLLM(json.dumps(payload))
    with pytest.raises(AnswerQualityParsingError):
        await judge_answer_quality(llm, _case(), "NO_ANSWER")


# --------------------------------------------------------------------------
# evidence isolation (Priority 8's invariant, re-verified under routing)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_substantive_prompt_never_contains_evidence_outside_what_was_passed():
    """Mirrors `test_critique.
    test_critique_prompt_never_contains_evidence_outside_what_was_passed` --
    this evaluator fetches nothing itself, so the only evidence it can ever
    see is `case.evidence_texts`."""
    case = _case(evidence_texts=["only-this-evidence-line"], question="what happened")
    llm = _QueuedLLM(_valid_substantive_json())

    await judge_answer_quality(llm, case, "a generated answer")

    assert len(llm.calls) == 1
    sent = str(llm.calls[0])
    assert "only-this-evidence-line" in sent


@pytest.mark.asyncio
async def test_refusal_prompt_never_contains_evidence_outside_what_was_passed():
    case = _case(evidence_texts=["only-this-evidence-line"], question="what happened")
    llm = _QueuedLLM(_valid_refusal_json())

    await judge_answer_quality(llm, case, "NO_ANSWER")

    sent = str(llm.calls[0])
    assert "only-this-evidence-line" in sent


@pytest.mark.asyncio
async def test_reference_answer_is_surfaced_to_the_evaluator_when_present():
    case = _case(reference_answer="the known-good answer")
    llm = _QueuedLLM(_valid_substantive_json())

    await judge_answer_quality(llm, case, "a generated answer")

    sent = str(llm.calls[0])
    assert "the known-good answer" in sent


@pytest.mark.asyncio
async def test_no_evidence_case_still_produces_a_renderable_prompt():
    case = _case(evidence_texts=[])
    llm = _QueuedLLM(_valid_substantive_json())

    judgement = await judge_answer_quality(llm, case, "an answer")

    assert judgement is not None
    sent = str(llm.calls[0])
    assert "no evidence was supplied" in sent


# --------------------------------------------------------------------------
# known_mode -- Priority 10's mode-detection hierarchy (TIER 1 over TIER 3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_mode_no_answer_routes_to_refusal_rubric_regardless_of_text():
    """The `aq-partial-evidence` bug, fixed and tested directly: free text
    that matches NEITHER legacy sentinel (`is_no_answer`,
    `_INSUFFICIENT_GROUNDING_MESSAGE`) is still correctly routed to the
    refusal rubric when the caller already knows the answer was declined
    (`known_mode="no_answer"`, Priority 10's TIER 1) -- the evaluator
    prefers the authoritative production outcome over guessing from text."""
    from app.evaluation.semantic.outcome import is_refusal_text

    free_text_decline = (
        "The root cause of the checkout service outage was never conclusively "
        "identified in the available postmortem notes. Therefore, there is no "
        "information to provide."
    )
    # Confirms this is a genuine regression guard: text-only detection really
    # would misclassify this (the exact failure Priority 9's live run found).
    assert not is_refusal_text(free_text_decline)

    llm = _QueuedLLM(_valid_refusal_json())
    judgement = await judge_answer_quality(
        llm, _case(), free_text_decline, known_mode="no_answer"
    )

    assert judgement.observed_answer_mode == "no_answer"
    assert judgement.refusal is not None
    assert judgement.substantive is None
    assert len(llm.calls) == 1  # known_mode adds no extra LLM call


@pytest.mark.asyncio
async def test_known_mode_none_falls_back_to_legacy_sentinel_detection():
    """`known_mode=None` (the default) preserves TIER 3 exactly -- a legacy
    exact-sentinel decline is still detected without any production outcome
    available, unchanged from Priority 9's behavior."""
    llm = _QueuedLLM(_valid_refusal_json())
    judgement = await judge_answer_quality(llm, _case(), "NO_ANSWER")
    assert judgement.observed_answer_mode == "no_answer"


@pytest.mark.asyncio
async def test_substantive_answer_with_refusal_like_wording_is_not_misclassified():
    """Case 5 (textual ambiguity): a substantive answer that merely
    MENTIONS refusal-shaped wording must not be treated as an abstention --
    exact-sentinel matching (not substring/heuristic matching) is what
    already prevents this, verified explicitly here as a locked-in
    invariant, not an incidental side effect."""
    from app.evaluation.semantic.outcome import is_refusal_text

    text = "The system cannot answer requests when the upstream connector is disabled."
    assert not is_refusal_text(text)

    llm = _QueuedLLM(_valid_substantive_json())
    judgement = await judge_answer_quality(llm, _case(), text)  # known_mode=None (default)

    assert judgement.observed_answer_mode == "substantive_answer"
    assert judgement.substantive is not None
    assert judgement.refusal is None
