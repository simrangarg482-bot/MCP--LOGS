"""The Answer Agent node (PROJECT_PLAN.md section 6.3 / AGENT_WORKFLOWS.md
section 2.3): reached only when `GraphState.route == "answer"`. Generates
`result.answer`/`result.citations`, applying grounding verification
(PROJECT_PLAN.md section 5.7) before anything reaches the caller.

Owned by: agents/answer/ -- a new subpackage not explicitly named in
PROJECT_PLAN.md section 10's file tree (which lists `graph.py`, `retrieval/`,
`investigation/`, `postmortem/`, `knowledge_gap/` only). Flagged as a
deliberate, defensible addition, not a silent deviation: the Answer Agent's
work here -- generation with citation markers, sentence-level grounding
verification with an embedding-then-LLM-escalation check, citation
extraction -- is comparable in size/shape to the Retrieval Agent's own
multi-file `agents/retrieval/` package, and cramming it into `graph.py`
alongside the state schema and node wiring would work against that file's
own stated purpose ("the composing layer").

Built as a factory (`make_answer_agent_node`), matching
`agents.retrieval.node`'s rationale for the same reason -- though this node
only needs `llm`, not an `AsyncSession`: retrieval and confidence evaluation
are already done by the time this node runs, and nothing here reads or
writes the database.

Failure handling per AGENT_WORKFLOWS.md section 2.3: an LLM timeout/rate-
limit, or an entirely ungrounded draft (every sentence fails verification),
is retried -- a fresh generation attempt, not a repaired one, since there is
no principled way to "fix" an ungrounded draft without regenerating it. Only
once `agents.retry.call_with_retry`'s budget is exhausted does this node fall
back to an explicit "insufficient grounded information" response, never a
partially-fabricated one.

`_generate_and_verify` also runs `agents.answer.sufficiency.assess_sufficiency`
BEFORE generation, not just grounding verification after -- see that module's
docstring for the confidently-wrong-answer failure mode (real, evaluated via
`scripts/eval_confidence.py`) that only checking sentences after the fact
against retrieved chunks cannot catch: a chunk can textually support a
sentence's wording while still not actually answering the question, either
because other retrieved chunks disagree with it or because it belongs to a
different, superficially-similar topic. A non-"sufficient" verdict fails
through the exact same retry-then-decline path an ungrounded draft already
does.

MACHINE-READABLE OUTCOME (Priority 10)
    `generate_answer_with_outcome` is this node's single authority for
    "was this question answered or correctly declined, and why" -- the
    exact sufficiency-check -> generate -> grounding-verify sequence
    `_generate_and_verify` already ran, refactored to RETURN an
    `AnswerOutcome` instead of only ever raising on decline. This exists
    because Priority 9's live semantic benchmark found a real correctness
    bug: calling `generate_answer` in isolation (bypassing this sequence
    entirely) can produce a free-text decline ("the root cause was never
    identified...") that doesn't match either legacy sentinel
    (`generation.is_no_answer`, `_INSUFFICIENT_GROUNDING_MESSAGE`), so
    downstream evaluation misclassified it. The fix is not a longer phrase
    list -- it's routing the caller through the same authoritative decision
    point production itself uses, so there is nothing left to guess from
    text. `_generate_and_verify` (below) is now a thin, retry-compatible
    wrapper around this function, preserving `node()`'s existing retry-on-
    any-outcome-including-decline behavior exactly (see its own docstring
    for why that must not change); `app.evaluation.semantic.runner` calls
    `generate_answer_with_outcome` directly, without that retry wrapping,
    since a benchmark case declining is itself a valid, measured outcome,
    not a failure to retry away.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from langchain_core.language_models import BaseChatModel

from app.agents.answer.citations import build_citations
from app.agents.answer.generation import generate_answer, is_no_answer
from app.agents.answer.grounding import split_sentences, verify_grounding
from app.agents.answer.markers import strip_markers
from app.agents.answer.sufficiency import assess_sufficiency
from app.agents.graph import GraphState
from app.agents.retry import call_with_retry
from app.core.memory.service import format_memory_context
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger
from app.shared.schemas import AskResponse, Citation

logger = get_logger(__name__)

_INSUFFICIENT_GROUNDING_MESSAGE = (
    "I don't have enough grounded information from the available sources to answer this "
    "confidently."
)

#: Internal-only, never serialized onto `AskResponse` (section 4.2: expose
#: the semantic outcome, not implementation internals) -- purpose is
#: logging/debugging and, for `no_answer`, letting a caller skip a redundant
#: legacy-sentinel/semantic classification step it would otherwise need
#: (see `app.evaluation.semantic.answer_quality.judge_answer_quality`'s
#: `known_mode` parameter).
AnswerOutcomeReason = Literal[
    "sufficiency_insufficient", "sufficiency_partial", "model_declined", "grounding_failed"
]


class AnswerOutcome(NamedTuple):
    """The single authoritative result of one answer-generation attempt.
    `mode="no_answer"` covers every decline path this node has -- evidence
    judged partial/insufficient before generation, the model explicitly
    declining, or a fully-generated draft losing every sentence to grounding
    verification -- collapsed to ONE outcome value, matching what
    `AskResponse.answer_mode` exposes: production does not currently
    distinguish these cases in its response shape (no qualified-answer mode
    exists, see that field's own docstring), so this type doesn't invent a
    finer public distinction than the product actually has. `reason` is kept
    for internal logging only.
    """

    mode: Literal["answered", "no_answer"]
    text: str
    citations: list[Citation]
    reason: AnswerOutcomeReason | None


class _UngroundedAnswerError(Exception):
    """Raised internally when a generated answer has no sentences left after
    grounding verification (or the model explicitly declined to answer).
    Caught by `agents.retry.call_with_retry` as a retryable failure, so a
    fresh generation attempt is made rather than reusing the ungrounded
    draft -- see module docstring.
    """


def make_answer_agent_node(
    llm: BaseChatModel,
) -> Callable[[GraphState], Awaitable[dict[str, Any]]]:
    """Build the LangGraph-callable Answer Agent node, bound to `llm`."""

    async def node(state: GraphState) -> dict[str, Any]:
        chunks = state.retrieved_chunks

        if not chunks:
            # Defensive: the Confidence Evaluation node should never route
            # here with zero retrieved chunks (a routing bug elsewhere would
            # be the real problem, not something this node can fix) -- but
            # there is nothing to ground an answer in either way, so this
            # degrades identically to exhausted grounding retries, without
            # spending an LLM call to discover that.
            return _insufficient_grounding_result(state)

        try:
            memory_context = format_memory_context(state.recalled_memories)
            grounded_text, citations = await call_with_retry(
                "answer_agent.generate",
                lambda: _generate_and_verify(
                    llm,
                    state.rewritten_query or state.query,
                    chunks,
                    memory_context=memory_context,
                ),
                retry_count=state.retry_count,
            )
        except Exception as exc:
            logger.warning("answer_agent_grounding_exhausted", query=state.query, error=str(exc))
            return _insufficient_grounding_result(state)

        result = AskResponse(
            confidence=state.confidence_score or 0.0,
            route_taken="answer",
            answer=grounded_text,
            answer_mode="answered",
            citations=citations,
        )
        return {"result": result, "retry_count": state.retry_count}

    return node


async def generate_answer_with_outcome(
    llm: BaseChatModel,
    query: str,
    chunks: list[ScoredChunk],
    *,
    memory_context: str = "",
) -> AnswerOutcome:
    """The single authoritative sufficiency-check -> generate ->
    grounding-verify sequence -- see module docstring's "MACHINE-READABLE
    OUTCOME" section for why this exists as its own function, returning a
    result rather than raising.

    The sufficiency check runs FIRST, not after grounding: grounding only
    ever checks whether a sentence the model already wrote is textually
    supported by *some* chunk, never whether the evidence as a whole
    actually answers `query` -- see `agents.answer.sufficiency`'s module
    docstring for the two real failure shapes this closes (cross-chunk
    conflict, topic-adjacent borrowing) that a purely post-generation check
    cannot catch.

    `chunks` must be non-empty -- callers with zero retrieved chunks should
    never reach here (see `node()`'s own guard); there is nothing to ground
    an answer in either way.
    """
    verdict = await assess_sufficiency(llm, query, chunks)
    if verdict != "sufficient":
        reason: AnswerOutcomeReason = (
            "sufficiency_partial" if verdict == "partial" else "sufficiency_insufficient"
        )
        return AnswerOutcome(
            mode="no_answer", text=_INSUFFICIENT_GROUNDING_MESSAGE, citations=[], reason=reason
        )

    raw_answer = await generate_answer(llm, query, chunks, memory_context=memory_context)
    if is_no_answer(raw_answer):
        return AnswerOutcome(
            mode="no_answer",
            text=_INSUFFICIENT_GROUNDING_MESSAGE,
            citations=[],
            reason="model_declined",
        )

    sentences = split_sentences(raw_answer)
    # Grounding is verified against `chunks` ONLY -- memory is deliberately
    # not passed here. A sentence must be traceable to real retrieved
    # evidence to survive; letting a recalled memory satisfy grounding would
    # turn memory into an unciteable evidence source and defeat the whole
    # point of the grounding gate (`agents.answer.grounding`).
    grounded_sentences = await verify_grounding(llm, sentences, chunks)
    if not grounded_sentences:
        # Precedence (section 4.3): sufficiency said "sufficient" and
        # generation produced text, but grounding is the LAST word -- the
        # final outcome here is "no_answer", never left as "answered" just
        # because an earlier stage was optimistic.
        return AnswerOutcome(
            mode="no_answer",
            text=_INSUFFICIENT_GROUNDING_MESSAGE,
            citations=[],
            reason="grounding_failed",
        )

    grounded_text = " ".join(grounded_sentences)
    # Citations are extracted from the marker-bearing text *before* stripping
    # markers for display -- see agents.answer.citations' module docstring on
    # why this ordering matters (a removed sentence must not contribute a
    # citation for a chunk it no longer cites, which is already guaranteed
    # here since `grounded_text` only contains surviving sentences).
    citations = build_citations(grounded_text, chunks)
    return AnswerOutcome(
        mode="answered", text=strip_markers(grounded_text), citations=citations, reason=None
    )


async def _generate_and_verify(
    llm: BaseChatModel,
    query: str,
    chunks: list[ScoredChunk],
    *,
    memory_context: str = "",
) -> tuple[str, list[Citation]]:
    """Thin, retry-compatible wrapper around `generate_answer_with_outcome`:
    raises `_UngroundedAnswerError` on `mode="no_answer"` so `node()`'s
    `call_with_retry` call keeps retrying on a decline exactly as it did
    before this function existed -- see module docstring for why that retry
    behavior must not change here, only be re-expressed on top of the new
    authoritative outcome.
    """
    outcome = await generate_answer_with_outcome(
        llm, query, chunks, memory_context=memory_context
    )
    if outcome.mode == "no_answer":
        raise _UngroundedAnswerError(outcome.reason or "declined")
    return outcome.text, outcome.citations


def _insufficient_grounding_result(state: GraphState) -> dict[str, Any]:
    result = AskResponse(
        confidence=state.confidence_score or 0.0,
        route_taken="answer",
        answer=_INSUFFICIENT_GROUNDING_MESSAGE,
        answer_mode="no_answer",
        citations=[],
    )
    return {"result": result, "retry_count": state.retry_count}
