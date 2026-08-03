"""Extracts `Citation`s from an answer's inline `[n]` markers
(AGENT_WORKFLOWS.md section 2.3 / API_DESIGN.md's `Citation` shape).

Owned by: agents/answer/. Operates on whatever text survives grounding
verification (`agents.answer.grounding`) -- a sentence removed for failing
grounding must not contribute a citation for a chunk it no longer actually
cites, so this must run *after* grounding, on the surviving text only.
"""

from __future__ import annotations

from app.agents.answer.markers import extract_citation_markers
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger
from app.shared.schemas import Citation

logger = get_logger(__name__)

_EXCERPT_MAX_CHARS = 300


def build_citations(text: str, chunks: list[ScoredChunk]) -> list[Citation]:
    """Build the `Citation` list for whichever `chunks` indices `text`
    actually references, via its `[n]` markers.

    Out-of-range marker numbers (the model citing `[7]` when only 5 context
    items existed) are logged and skipped rather than raising -- a malformed
    citation must not crash the whole response.
    """
    citations: list[Citation] = []
    for number in extract_citation_markers(text):
        index = number - 1
        if index < 0 or index >= len(chunks):
            logger.warning(
                "answer_agent_invalid_citation_marker", marker=number, chunk_count=len(chunks)
            )
            continue
        chunk = chunks[index]
        excerpt = chunk.content[:_EXCERPT_MAX_CHARS]
        if len(chunk.content) > _EXCERPT_MAX_CHARS:
            excerpt += "..."
        citations.append(
            Citation(
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                source_url=chunk.source_url,
                excerpt=excerpt,
            )
        )
    return citations
