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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.agents.answer.citations import build_citations
from app.agents.answer.generation import generate_answer, is_no_answer
from app.agents.answer.grounding import split_sentences, verify_grounding
from app.agents.answer.markers import strip_markers
from app.agents.answer.sufficiency import assess_sufficiency
from app.agents.graph import GraphState
from app.agents.retry import call_with_retry
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger
from app.shared.schemas import AskResponse, Citation

logger = get_logger(__name__)

_INSUFFICIENT_GROUNDING_MESSAGE = (
    "I don't have enough grounded information from the available sources to answer this confidently."
)


class _UngroundedAnswerError(Exception):
    """Raised internally when a generated answer has no sentences left after grounding verification (or the model explicitly declined to answer). Caught by `agents.retry.call_with_retry` as a retryable failure, so a fresh generation attempt is made rather than reusing the ungrounded draft -- see module docstring.
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
            grounded_text, citations = await call_with_retry(
                "answer_agent.generate",
                lambda: _generate_and_verify(llm, state.rewritten_query or state.query, chunks),
                retry_count=state.retry_count,
            )
        except Exception as exc:
            logger.warning("answer_agent_grounding_exhausted", query=state.query, error=str(exc))
            return _insufficient_grounding_result(state)

        result = AskResponse(
            confidence=state.confidence_score or 0.0,
            route_taken="answer",
            answer=grounded_text,
            citations=citations,
        )
        return {"result": result, "retry_count": state.retry_count}

    return node


async def _generate_and_verify(
    llm: BaseChatModel, query: str, chunks: list[ScoredChunk]
) -> tuple[str, list[Citation]]:
    """One full sufficiency-check-then-generate-then-verify attempt. Raises
    `_UngroundedAnswerError` if the evidence is judged partial/insufficient
    before generation even runs, if the model declined to answer, or if
    nothing survives grounding verification -- every case flows through the
    same retryable-failure path, so `call_with_retry` treats them
    identically and tries a fresh attempt.

    The sufficiency check runs FIRST, not after grounding: grounding only
    ever checks whether a sentence the model already wrote is textually
    supported by *some* chunk, never whether the evidence as a whole
    actually answers `query` -- see `agents.answer.sufficiency`'s module
    docstring for the two real failure shapes this closes (cross-chunk
    conflict, topic-adjacent borrowing) that a purely post-generation check
    cannot catch.
    """
    verdict = await assess_sufficiency(llm, query, chunks)
    if verdict != "sufficient":
        raise _UngroundedAnswerError(f"evidence sufficiency check: {verdict}")

    raw_answer = await generate_answer(llm, query, chunks)
    if is_no_answer(raw_answer):
        raise _UngroundedAnswerError("model declined to answer from context")

    sentences = split_sentences(raw_answer)
    grounded_sentences = await verify_grounding(llm, sentences, chunks)
    if not grounded_sentences:
        raise _UngroundedAnswerError("no sentence survived grounding verification")

    grounded_text = " ".join(grounded_sentences)
    # Citations are extracted from the marker-bearing text *before* stripping
    # markers for display -- see agents.answer.citations' module docstring on
    # why this ordering matters (a removed sentence must not contribute a
    # citation for a chunk it no longer cites, which is already guaranteed
    # here since `grounded_text` only contains surviving sentences).
    citations = build_citations(grounded_text, chunks)
    return strip_markers(grounded_text), citations


def _insufficient_grounding_result(state: GraphState) -> dict[str, Any]:
    result = AskResponse(
        confidence=state.confidence_score or 0.0,
        route_taken="answer",
        answer=_INSUFFICIENT_GROUNDING_MESSAGE,
        citations=[],
    )
    return {"result": result, "retry_count": state.retry_count}
