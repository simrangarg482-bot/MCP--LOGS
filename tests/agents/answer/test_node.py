"""Tests for `app.agents.answer.node` -- Priority 10's machine-readable
`AnswerOutcome`/`generate_answer_with_outcome`, the single authority for
"was this question answered or correctly declined, and why." No dedicated
test file existed for this module before this priority (only `test_
generation.py` covers the lower-level `generate_answer`); this is the
first direct coverage of the sufficiency -> generate -> grounding sequence
and the node-level `answer_mode` propagation onto `AskResponse`.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.answer.node import (
    _INSUFFICIENT_GROUNDING_MESSAGE,
    _generate_and_verify,
    _UngroundedAnswerError,
    generate_answer_with_outcome,
    make_answer_agent_node,
)
from app.agents.graph import GraphState
from app.retrieval.schemas import ScoredChunk
from app.shared.schemas import Identity


def _chunk(content: str, title: str = "Some Doc") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="documentation",
        content=content,
        score=0.9,
        source_offset_start=0,
        source_offset_end=len(content),
        title=title,
    )


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _QueuedLLM:
    """Returns one queued response per `.ainvoke()` call, in order --
    matches the convention already established in `tests/agents/
    investigation/test_critique.py`."""

    def __init__(self, *replies: str) -> None:
        self._queue = list(replies)
        self.calls: list[object] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._queue:
            raise AssertionError("LLM called more times than responses were queued")
        return _Response(self._queue.pop(0))


def _sufficient() -> str:
    return "Step 1: the fact.\nStep 2: yes.\nStep 3: no conflict.\nVERDICT: SUFFICIENT"


def _partial() -> str:
    return "Step 1: the fact.\nStep 2: no, only on-topic.\nVERDICT: PARTIAL"


def _insufficient() -> str:
    return "Step 1: the fact.\nStep 2: no bearing at all.\nVERDICT: INSUFFICIENT"


async def _always_grounded_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0] for _ in texts]


async def _always_ungrounded_embed_toggle():
    """Returns a stateful fake: first call (sentences) -> [1.0], second
    call (chunks) -> [-1.0], so every sentence's max cosine similarity
    against every chunk is -1.0 -- below `_UNGROUNDED_THRESHOLD`, no LLM
    escalation call needed."""
    call_count = 0

    async def _fake(texts: list[str]) -> list[list[float]]:
        nonlocal call_count
        call_count += 1
        value = 1.0 if call_count == 1 else -1.0
        return [[value] for _ in texts]

    return _fake


# --------------------------------------------------------------------------
# generate_answer_with_outcome -- the single authority
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sufficient_evidence_and_grounded_answer_produces_answered_outcome(monkeypatch):
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)

    llm = _QueuedLLM(_sufficient(), "The root cause was X [1].")
    chunks = [_chunk("evidence about X")]

    outcome = await generate_answer_with_outcome(llm, "why did it break?", chunks)

    assert outcome.mode == "answered"
    assert outcome.reason is None
    assert "root cause was X" in outcome.text
    assert len(llm.calls) == 2  # sufficiency + generation, no grounding LLM escalation needed


@pytest.mark.asyncio
async def test_insufficient_evidence_short_circuits_before_generation():
    llm = _QueuedLLM(_insufficient())  # only ONE reply queued -- generation must not be called
    chunks = [_chunk("unrelated evidence")]

    outcome = await generate_answer_with_outcome(llm, "why did it break?", chunks)

    assert outcome.mode == "no_answer"
    assert outcome.reason == "sufficiency_insufficient"
    assert outcome.text == _INSUFFICIENT_GROUNDING_MESSAGE
    assert outcome.citations == []
    assert len(llm.calls) == 1  # generation never ran


@pytest.mark.asyncio
async def test_partial_evidence_short_circuits_before_generation():
    llm = _QueuedLLM(_partial())
    chunks = [_chunk("on-topic but not specific evidence")]

    outcome = await generate_answer_with_outcome(llm, "what exact version?", chunks)

    assert outcome.mode == "no_answer"
    assert outcome.reason == "sufficiency_partial"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_model_declining_after_sufficient_verdict_is_no_answer():
    llm = _QueuedLLM(_sufficient(), "NO_ANSWER")
    chunks = [_chunk("evidence")]

    outcome = await generate_answer_with_outcome(llm, "why did it break?", chunks)

    assert outcome.mode == "no_answer"
    assert outcome.reason == "model_declined"
    assert outcome.text == _INSUFFICIENT_GROUNDING_MESSAGE


@pytest.mark.asyncio
async def test_late_grounding_failure_overrides_an_earlier_sufficient_verdict(monkeypatch):
    """Precedence (section 4.3): sufficiency said SUFFICIENT and generation
    produced text, but grounding is the last word -- the final outcome must
    be `no_answer`, never left as `answered` because an earlier stage was
    optimistic."""
    fake_embed = await _always_ungrounded_embed_toggle()
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", fake_embed)

    llm = _QueuedLLM(_sufficient(), "A confident claim [1].")
    chunks = [_chunk("some evidence")]

    outcome = await generate_answer_with_outcome(llm, "why did it break?", chunks)

    assert outcome.mode == "no_answer"
    assert outcome.reason == "grounding_failed"
    assert outcome.text == _INSUFFICIENT_GROUNDING_MESSAGE


# --------------------------------------------------------------------------
# _generate_and_verify -- retry-compatible wrapper, must not change
# node()'s existing retry-on-decline behavior
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_verify_raises_on_decline_for_retry_compatibility():
    llm = _QueuedLLM(_insufficient())
    with pytest.raises(_UngroundedAnswerError):
        await _generate_and_verify(llm, "q", [_chunk("evidence")])


@pytest.mark.asyncio
async def test_generate_and_verify_returns_tuple_on_success(monkeypatch):
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)
    llm = _QueuedLLM(_sufficient(), "An answer [1].")
    text, citations = await _generate_and_verify(llm, "q", [_chunk("evidence")])
    assert "An answer" in text


# --------------------------------------------------------------------------
# node() -- answer_mode propagation onto AskResponse
# --------------------------------------------------------------------------


def _state(query: str = "why?", chunks: list[ScoredChunk] | None = None) -> GraphState:
    return GraphState(
        query=query,
        actor=Identity.for_agent("test_actor", uuid.uuid4()),
        retrieved_chunks=chunks or [],
    )


@pytest.mark.asyncio
async def test_node_sets_answer_mode_answered_on_success(monkeypatch):
    monkeypatch.setattr("app.retrieval.embedding.embed_texts", _always_grounded_embed)
    llm = _QueuedLLM(_sufficient(), "A grounded answer [1].")
    node = make_answer_agent_node(llm)

    result = await node(_state(chunks=[_chunk("evidence")]))

    assert result["result"].answer_mode == "answered"
    assert result["result"].route_taken == "answer"


@pytest.mark.asyncio
async def test_node_sets_answer_mode_no_answer_on_zero_chunks():
    llm = _QueuedLLM()  # no calls expected
    node = make_answer_agent_node(llm)

    result = await node(_state(chunks=[]))

    assert result["result"].answer_mode == "no_answer"
    assert result["result"].answer == _INSUFFICIENT_GROUNDING_MESSAGE
    assert llm.calls == []


@pytest.mark.asyncio
async def test_node_sets_answer_mode_no_answer_after_retries_exhausted():
    # 3 attempts (call_with_retry's own bound), each declines with INSUFFICIENT
    # -- generation never runs for any attempt.
    llm = _QueuedLLM(_insufficient(), _insufficient(), _insufficient())
    node = make_answer_agent_node(llm)

    result = await node(_state(chunks=[_chunk("weak evidence")]))

    assert result["result"].answer_mode == "no_answer"
    assert result["result"].answer == _INSUFFICIENT_GROUNDING_MESSAGE
