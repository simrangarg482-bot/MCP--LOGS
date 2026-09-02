"""`retrieval.service.search_with_signals` -- the entry point the Retrieval
Agent uses so the Confidence node can read a real top-similarity magnitude
instead of the near-constant fused RRF score it used to get (EKIP audit
2026-09-02, finding 2).

The fused `chunks` list must stay byte-for-byte what `search()` would
return; the added `top_dense_similarity` must come from the *dense* list's
best hit (not the fused list, whose scores are rank-based), and be `None`
when dense search found nothing.
"""

from __future__ import annotations

import uuid

import pytest

from app.retrieval import service
from app.retrieval.schemas import ScoredChunk, SearchFilters

_FILTERS = SearchFilters(organization_id=uuid.uuid4())


def _chunk(content: str, score: float, collection: str = "documentation") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection=collection,
        content=content,
        score=score,
        source_offset_start=0,
        source_offset_end=len(content),
    )


@pytest.fixture
def _patched_store(monkeypatch):
    """Stub the embedding call and both store queries; leave the real
    `reciprocal_rank_fusion` in place.
    """

    async def _fake_embed_query(_query: str) -> list[float]:
        return [0.1] * 384

    monkeypatch.setattr(service.embedding, "embed_query", _fake_embed_query)

    def _install(dense: list[ScoredChunk], lexical: list[ScoredChunk]) -> None:
        async def _fake_search_all(*_args, **_kwargs):
            return list(dense)

        async def _fake_lexical_search_all(*_args, **_kwargs):
            return list(lexical)

        monkeypatch.setattr(service._store, "search_all", _fake_search_all)
        monkeypatch.setattr(service._store, "lexical_search_all", _fake_lexical_search_all)

    return _install


async def test_top_dense_similarity_is_the_best_dense_score(_patched_store) -> None:
    dense = [_chunk("best dense", 0.63), _chunk("second", 0.41)]
    lexical = [_chunk("lexical only", 0.02)]
    _patched_store(dense, lexical)

    result = await service.search_with_signals(None, "why did it fail?", _FILTERS, top_k=10)

    assert result.top_dense_similarity == pytest.approx(0.63)


async def test_top_dense_similarity_is_none_when_dense_search_is_empty(_patched_store) -> None:
    _patched_store(dense=[], lexical=[_chunk("lexical hit", 0.5)])

    result = await service.search_with_signals(None, "q", _FILTERS, top_k=10)

    assert result.top_dense_similarity is None
    # a lexical-only hit still comes back in the fused chunk list
    assert [c.content for c in result.chunks] == ["lexical hit"]


async def test_chunks_are_the_rrf_fused_result_not_the_raw_dense_list(_patched_store) -> None:
    shared_id = uuid.uuid4()
    in_both_dense = ScoredChunk(
        chunk_id=shared_id,
        document_id=uuid.uuid4(),
        collection="documentation",
        content="agreed by both",
        score=0.55,
        source_offset_start=0,
        source_offset_end=14,
    )
    dense_only = _chunk("dense only, ranked #1 by similarity", 0.99)
    in_both_lexical = in_both_dense.model_copy(update={"score": 0.7})

    _patched_store(dense=[dense_only, in_both_dense], lexical=[in_both_lexical])

    result = await service.search_with_signals(None, "q", _FILTERS, top_k=10)

    # RRF rewards cross-list agreement: the chunk both lists surfaced wins,
    # even though `dense_only` was the #1 dense hit. (If `chunks` were just
    # the dense list, `dense_only` would be first.)
    assert result.chunks[0].chunk_id == shared_id
    # ...but the similarity signal still reflects the strongest dense match.
    assert result.top_dense_similarity == pytest.approx(0.99)
