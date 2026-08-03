"""Reciprocal rank fusion (RRF) -- combines multiple independently-ranked
`ScoredChunk` lists (PROJECT_PLAN.md section 5.2: dense + BM25/lexical,
across every collection) into one fused ranking.

Owned by: retrieval/ranking/. Used only by `retrieval/service.py`'s
`search()` -- not part of the `VectorStore` protocol itself, since fusion
happens *across* both of a backend's search methods (and across every
collection), not inside either one.

RRF is rank-based, not score-based: it never compares pgvector's negated
inner-product distance against Postgres's `ts_rank_cd` value directly (the
two are on entirely different, incomparable scales). Each list only
contributes each chunk's *position* within that list -- this is precisely
why RRF, rather than a weighted sum of raw scores, is the standard technique
for merging heterogeneous retrieval signals (PROJECT_PLAN.md section 5.2).
"""

from __future__ import annotations

import uuid

from app.retrieval.schemas import ScoredChunk

# Standard RRF damping constant (Cormack, Clarke & Buettcher 2009). Large
# enough that a chunk's exact rank near the top of a list matters less than
# simply *appearing* near the top of multiple lists -- the intended
# reinforcement effect: a chunk both dense and lexical search independently
# consider relevant should outrank one only one method found, even if that
# one method ranked it #1.
_DEFAULT_K = 60


def reciprocal_rank_fusion(
    result_lists: list[list[ScoredChunk]], *, top_k: int, k: int = _DEFAULT_K
) -> list[ScoredChunk]:
    """Fuse multiple ranked `ScoredChunk` lists into one, keyed by
    `chunk_id`.

    Each list contributes `1 / (k + rank)` to a chunk's fused score, with
    `rank` counted from 1 within that list; a chunk absent from a given list
    simply contributes nothing for that list (not a penalty). A chunk that
    appears in more than one list (the common, desired case: both dense and
    lexical search surfaced it) accumulates the sum of its per-list
    contributions, so agreement across retrieval modes is rewarded.

    The returned `ScoredChunk` for a given `chunk_id` reuses whichever
    occurrence was encountered first across `result_lists` (the same
    underlying row, so `content`/`title`/`source_url`/offsets are identical
    regardless of which list it came from) with only `score` replaced by
    the fused RRF value. Results are sorted by fused score descending and
    truncated to `top_k`.
    """
    fused_scores: dict[uuid.UUID, float] = {}
    representative_chunks: dict[uuid.UUID, ScoredChunk] = {}

    for result_list in result_lists:
        for rank, chunk in enumerate(result_list, start=1):
            fused_scores[chunk.chunk_id] = fused_scores.get(chunk.chunk_id, 0.0) + 1.0 / (
                k + rank
            )
            representative_chunks.setdefault(chunk.chunk_id, chunk)

    ranked_ids = sorted(fused_scores, key=lambda chunk_id: fused_scores[chunk_id], reverse=True)

    return [
        representative_chunks[chunk_id].model_copy(update={"score": fused_scores[chunk_id]})
        for chunk_id in ranked_ids[:top_k]
    ]
