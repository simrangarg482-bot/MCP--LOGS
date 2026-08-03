"""Cross-encoder reranking -- stage 3 of the Retrieval Agent
(AGENT_WORKFLOWS.md section 2.1 step 3 / PROJECT_PLAN.md section 5.3):
re-scores the RRF-fused candidate set with a more expensive, more accurate
model than the initial retrieval pass.

Owned by: agents/retrieval/, NOT app/retrieval/ -- per PROJECT_PLAN.md
section 5.3's own framing ("a standard two-stage retrieval pattern": initial
retrieval optimizes recall cheaply over a large corpus, reranking optimizes
precision expensively over a small candidate set) and the naming-collision
note in section 10: reranking is agent-facing refinement of results
`retrieval.service.search()` (dense + lexical + RRF fusion, task #16) has
already returned, not a capability of the storage-agnostic `VectorStore`
abstraction itself. `app/retrieval/ranking/` holds only RRF fusion.

Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (ENGINEERING_DECISIONS.md
#009) -- a small (~80MB), CPU-friendly, widely-used MS MARCO-trained
cross-encoder. No new dependency: `sentence-transformers` (already pinned
per #006) provides `CrossEncoder` directly.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from sentence_transformers import CrossEncoder

from app.retrieval.schemas import ScoredChunk

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # ENGINEERING_DECISIONS.md #009


@lru_cache
def _get_model() -> CrossEncoder:
    """Load the cross-encoder once per process and cache it -- same
    load-once-per-process singleton pattern as
    `app.retrieval.embedding._get_model`.
    """
    return CrossEncoder(_MODEL_NAME)


async def rerank(query: str, chunks: list[ScoredChunk], *, top_k: int) -> list[ScoredChunk]:
    """Re-score `chunks` against `query` with the cross-encoder, returning at
    most `top_k` chunks ordered by rerank score descending.

    `chunks` is expected to already be the fused candidate set from
    `retrieval.service.search()` -- reranking narrows and re-orders it, it
    never fetches anything new. Each returned chunk's `score` is overwritten
    with its cross-encoder score: the RRF score that got it into this
    candidate set has already served its purpose (being selected as a
    candidate) and is not on a comparable scale to a cross-encoder score
    anyway.
    """
    if not chunks:
        return []

    model = _get_model()
    pairs = [(query, chunk.content) for chunk in chunks]
    raw_scores = await asyncio.to_thread(model.predict, pairs)

    reranked = sorted(
        (
            chunk.model_copy(update={"score": float(score)})
            for chunk, score in zip(chunks, raw_scores, strict=True)
        ),
        key=lambda scored_chunk: scored_chunk.score,
        reverse=True,
    )
    return reranked[:top_k]
