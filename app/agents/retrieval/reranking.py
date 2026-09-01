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
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # ENGINEERING_DECISIONS.md #009

# Failures that mean "this process cannot run the cross-encoder right now"
# rather than "the query is bad": the weights download/mmap failed, the CPU
# math library (OpenBLAS/MKL) could not allocate its scratch buffers under
# memory pressure, or torch raised its own OOM. Reranking is precision
# refinement over an already recall-complete candidate set (see the module
# docstring and PROJECT_PLAN.md section 5.3), so none of these should take
# down the whole answer/investigation -- `agents.retrieval.node` already
# degrades the same way when hybrid search itself is exhausted.
#
# (A hard native crash during model load -- e.g. a Windows access violation
# from safetensors mmap failing mid-materialization -- is below Python's
# exception machinery and cannot be caught here; a process that cannot
# afford the second model at all should set `agent_reranking_enabled=false`
# so it is never loaded.)
_RERANK_UNAVAILABLE_ERRORS = (OSError, RuntimeError, MemoryError, ImportError)

# Score stamped on chunks returned without a real cross-encoder pass (the
# `agent_reranking_enabled=false` path and the catchable-failure fallback
# below). The Confidence node reads the top retrieved chunk's `score` as its
# `rerank_score` signal and calibrates it as an MS-MARCO logit
# (`agents.confidence._normalize_rerank_score`, centered at -8.0); leaving a
# raw RRF fused score (~0.02) there would be read as a strongly positive
# logit and *inflate* confidence. -8.0 is that calibration's documented
# "borderline" value -> a neutral 0.5 rerank signal, neither rewarding nor
# penalizing evidence that was never actually reranked.
_UNRANKED_NEUTRAL_SCORE = -8.0


def fused_order_fallback(chunks: list[ScoredChunk], *, top_k: int) -> list[ScoredChunk]:
    """The first `top_k` candidates in their existing RRF-fused order, with a
    neutral `score` so the Confidence node does not misread the RRF score as
    a cross-encoder logit. Shared by the disabled path
    (`agents.retrieval.node`) and `rerank`'s own failure fallback.
    """
    return [
        chunk.model_copy(update={"score": _UNRANKED_NEUTRAL_SCORE})
        for chunk in chunks[:top_k]
    ]


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

    try:
        model = _get_model()
        pairs = [(query, chunk.content) for chunk in chunks]
        raw_scores = await asyncio.to_thread(model.predict, pairs)
    except _RERANK_UNAVAILABLE_ERRORS as exc:
        # Fall back to the candidate set's existing (RRF-fused) order rather
        # than failing the caller -- reranking only refines precision, the
        # fused set is already recall-complete and ranked.
        logger.warning(
            "rerank_unavailable_falling_back_to_fused_order",
            error=str(exc),
            error_type=type(exc).__name__,
            candidate_count=len(chunks),
        )
        return fused_order_fallback(chunks, top_k=top_k)

    reranked = sorted(
        (
            chunk.model_copy(update={"score": float(score)})
            for chunk, score in zip(chunks, raw_scores, strict=True)
        ),
        key=lambda scored_chunk: scored_chunk.score,
        reverse=True,
    )
    return reranked[:top_k]
