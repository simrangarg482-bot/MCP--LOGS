"""The Retrieval Agent must not fail an answer/investigation just because the
cross-encoder reranker cannot run in this process -- reranking is precision
refinement over an already recall-complete, RRF-ordered candidate set
(PROJECT_PLAN.md section 5.3). Covers both the catchable-failure fallback
inside `rerank` and the shared `fused_order_fallback` the
`agent_reranking_enabled=false` path uses.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.retrieval import reranking
from app.agents.retrieval.reranking import (
    _UNRANKED_NEUTRAL_SCORE,
    fused_order_fallback,
    rerank,
)
from app.retrieval.schemas import ScoredChunk


def _chunk(rank: int, score: float) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="code",
        content=f"candidate chunk {rank}",
        score=score,
        source_offset_start=0,
        source_offset_end=10,
    )


def test_fused_order_fallback_preserves_order_and_neutralizes_score() -> None:
    candidates = [_chunk(0, 0.030), _chunk(1, 0.020), _chunk(2, 0.016)]

    out = fused_order_fallback(candidates, top_k=2)

    assert [c.content for c in out] == ["candidate chunk 0", "candidate chunk 1"]
    # A raw RRF score (~0.02) left here would be read by the Confidence node
    # as a strongly positive cross-encoder logit and inflate confidence.
    assert all(c.score == _UNRANKED_NEUTRAL_SCORE for c in out)


@pytest.mark.asyncio
async def test_rerank_falls_back_to_fused_order_when_model_unavailable(monkeypatch) -> None:
    def _boom() -> object:
        raise OSError("The paging file is too small for this operation to complete")

    monkeypatch.setattr(reranking, "_get_model", _boom)
    candidates = [_chunk(0, 0.030), _chunk(1, 0.020), _chunk(2, 0.016)]

    out = await rerank("some query", candidates, top_k=2)

    assert [c.content for c in out] == ["candidate chunk 0", "candidate chunk 1"]
    assert all(c.score == _UNRANKED_NEUTRAL_SCORE for c in out)


@pytest.mark.asyncio
async def test_rerank_still_returns_empty_for_no_candidates(monkeypatch) -> None:
    monkeypatch.setattr(reranking, "_get_model", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert await rerank("q", [], top_k=5) == []


@pytest.mark.asyncio
async def test_rerank_reraises_non_model_errors(monkeypatch) -> None:
    class _Model:
        def predict(self, pairs):  # noqa: ANN001, D401
            raise ValueError("genuinely bad input, not a resource problem")

    monkeypatch.setattr(reranking, "_get_model", _Model)
    with pytest.raises(ValueError):
        await rerank("q", [_chunk(0, 0.03)], top_k=1)
