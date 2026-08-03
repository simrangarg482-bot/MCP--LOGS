"""Answer generation -- the Answer Agent's core step (AGENT_WORKFLOWS.md
section 2.3 / PROJECT_PLAN.md section 6.3): generate a response constrained
to `retrieved_chunks` only, with inline `[n]` citation markers the grounding
verification and citation-extraction steps both depend on.

Owned by: agents/answer/. Reached only when `GraphState.route == "answer"`
(the graph wiring, task #21, enforces this; this module has no routing logic
of its own).
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.retrieval.schemas import ScoredChunk

# A literal sentinel the model is instructed to return verbatim when the
# context genuinely doesn't answer the question -- distinguished from a
# normal answer without needing a second classification call.
_NO_ANSWER_MARKER = "NO_ANSWER"


def build_context_block(chunks: list[ScoredChunk]) -> str:
    """Render `chunks` as a numbered context block, 1-indexed -- the same
    numbering the generation prompt asks the model to cite with (`[1]`,
    `[2]`, ...) and `citations.extract_citation_markers` later parses back
    out.
    """
    lines = []
    for index, chunk in enumerate(chunks, start=1):
        title = chunk.title or "untitled"
        lines.append(f"[{index}] ({title}): {chunk.content}")
    return "\n\n".join(lines)


async def generate_answer(llm: BaseChatModel, query: str, chunks: list[ScoredChunk]) -> str:
    """Generate the raw answer text, with inline `[n]` citation markers.

    `chunks` must be non-empty -- callers with zero retrieved chunks should
    never reach this function (see `answer.node`'s guard); there is nothing
    for the model to be constrained to otherwise.
    """
    context_block = build_context_block(chunks)
    prompt = (
        "You are answering an engineer's question using ONLY the numbered "
        "context below. Do not use any outside knowledge. Every factual "
        "claim must be immediately followed by the bracketed number(s) of "
        "the context item(s) that support it, placed before the sentence's "
        "ending punctuation, e.g. 'The service restarts automatically [2].' "
        "If the context does not contain enough information to answer, "
        f"respond with exactly '{_NO_ANSWER_MARKER}' and nothing else -- do "
        "not guess or use outside/general knowledge.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {query}"
    )
    response = await llm.ainvoke(prompt)
    return str(response.content).strip()


def is_no_answer(raw_answer: str) -> bool:
    """Whether the model explicitly declined to answer (see
    `_NO_ANSWER_MARKER`'s prompt instruction above).
    """
    return raw_answer.strip() == _NO_ANSWER_MARKER
