"""Pluggable semantic-similarity scoring, for the `semantic_similarity`
answer assertion and any future semantic grounding evaluator.

`SimilarityScorer` is a `typing.Protocol` (same composition-over-inheritance
convention as `app.ingestion.connectors.base.Connector` and
`app.retrieval.interfaces.base.VectorStore` -- see either's docstring for
why this codebase prefers structural typing here over an ABC).

`TokenOverlapSimilarityScorer` is the deterministic Mode 1 default: no
model, no network call, pure string math -- exactly the "must not require a
paid API for normal tests" / "deterministic fallback" requirement. It is
deliberately crude (Jaccard similarity over lowercased word sets) and is not
meant to approximate real semantic similarity well; it exists so
`semantic_similarity` assertions have *some* runnable default rather than
being unusable outside live mode.

`EmbeddingSimilarityScorer` wraps EKIP's own real, local (not paid --
`sentence-transformers`, per `app.retrieval.embedding`'s module docstring)
embedding model for a genuinely meaningful score, for callers in Mode 2/3
who want it. Not the Mode 1 default because loading the embedding model
is real per-process overhead this package doesn't want to force onto every
unit test that happens to touch an answer assertion.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.evaluation.schemas import normalize_text


@runtime_checkable
class SimilarityScorer(Protocol):
    async def similarity(self, text_a: str, text_b: str) -> float:
        """Return a similarity score in `[0.0, 1.0]` (implementations should
        clamp their own output into this range)."""
        ...


class TokenOverlapSimilarityScorer:
    """Deterministic Jaccard-similarity-over-words fallback. See module
    docstring for why this exists and what it is not meant to be.
    """

    async def similarity(self, text_a: str, text_b: str) -> float:
        words_a = set(normalize_text(text_a).split())
        words_b = set(normalize_text(text_b).split())
        if not words_a and not words_b:
            return 1.0
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)


class EmbeddingSimilarityScorer:
    """Real, local embedding-based cosine similarity via
    `app.retrieval.embedding` -- opt-in (Mode 2/3), never the Mode 1 default.
    """

    async def similarity(self, text_a: str, text_b: str) -> float:
        from app.retrieval import embedding  # deferred: avoid loading the model at import time

        vectors = await embedding.embed_texts([text_a, text_b])
        vector_a, vector_b = vectors[0], vectors[1]
        # Both vectors are already L2-normalized by `embed_texts`
        # (`normalize_embeddings=True`, per `app.agents.answer.grounding`'s
        # identical reasoning for the same computation), so a plain dot
        # product is cosine similarity.
        dot_product = sum(x * y for x, y in zip(vector_a, vector_b, strict=True))
        return max(0.0, min(1.0, dot_product))


DEFAULT_SIMILARITY_SCORER: SimilarityScorer = TokenOverlapSimilarityScorer()
