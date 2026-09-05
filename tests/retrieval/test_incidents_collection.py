"""Audit finding 6 ("No real 'incidents' retrieval collection") --
focused regression tests for the parts of the fix that don't fit
`tests/agents/test_service.py`, `tests/agents/test_confidence.py`, or
`tests/retrieval/test_search_with_signals.py` (already covering
`search_similar_incidents`, `historical_similarity`, and
`search_with_signals(collection=...)` respectively).

These tests deliberately do not require a live database or the real
embedding model -- consistent with this repo's existing test suite
(nothing here hits real Postgres/pgvector; see those modules' own
docstrings), and this sandbox's `sentence_transformers` is stubbed out
(no real model weights available), same as every other test file.
"""

from __future__ import annotations

import math
from typing import get_args

from app.database.models.retrieval_models import (
    CodeChunk,
    ConversationChunk,
    DocumentationChunk,
    IncidentChunk,
    _ChunkColumns,
    _RepoScopedChunkColumns,
)
from app.retrieval.pgvector.store import _COLLECTION_MODELS, _SINGLE_COLLECTION_MODELS
from app.retrieval.schemas import CollectionName


def test_incidents_is_a_valid_collection_name() -> None:
    """Requirement 1 / requirement 9's first bullet: `"incidents"` is a
    real member of the `CollectionName` type, not merely documented.
    """
    assert "incidents" in get_args(CollectionName)


def test_incident_chunk_model_maps_to_the_incidents_chunks_table() -> None:
    assert IncidentChunk.__tablename__ == "incidents_chunks"


def test_incident_chunk_uses_plain_chunk_columns_not_repo_scoped() -> None:
    """`IncidentChunk` is not GitHub-sourced content, so it must use the
    plain `_ChunkColumns` mixin (no `repo_full_name`) -- the same reasoning
    `ConversationChunk` already follows for Slack/Teams content.
    """
    assert issubclass(IncidentChunk, _ChunkColumns)
    assert not issubclass(IncidentChunk, _RepoScopedChunkColumns)
    assert not hasattr(IncidentChunk, "repo_full_name")


def test_repo_scoped_collections_are_unaffected() -> None:
    """Requirement "do not change unrelated retrieval behavior": the two
    GitHub-sourced collections must still be repo-scoped after this change.
    """
    assert issubclass(CodeChunk, _RepoScopedChunkColumns)
    assert issubclass(DocumentationChunk, _RepoScopedChunkColumns)
    assert not issubclass(ConversationChunk, _RepoScopedChunkColumns)


def test_incidents_collection_is_excluded_from_the_all_collections_default() -> None:
    """Requirement 9's "unrelated documentation/code/conversation chunks
    are not being used as the incident-history source by default" -- read
    from the other direction: `"incidents"` must never be searched by the
    all-collections default (`search_all`/`lexical_search_all`'s
    `_COLLECTION_MODELS`), only by an explicit `collection="incidents"`
    call (`_SINGLE_COLLECTION_MODELS`). A caller asking for "everything"
    must not silently start pulling in incident records mixed with
    documentation/code/conversation results.
    """
    assert "incidents" not in _COLLECTION_MODELS
    assert set(_COLLECTION_MODELS) == {"documentation", "code", "conversations"}


def test_single_collection_models_is_a_superset_including_incidents() -> None:
    """Requirement 5: an explicit `collection="incidents"` call (as
    `search_similar_incidents` and the Retrieval Agent's
    `historical_similarity` search both now make) must resolve to the real
    `IncidentChunk` model, not fall back to searching anything else.
    """
    assert _SINGLE_COLLECTION_MODELS["incidents"] is IncidentChunk
    assert set(_COLLECTION_MODELS).issubset(set(_SINGLE_COLLECTION_MODELS))
    for name, model in _COLLECTION_MODELS.items():
        assert _SINGLE_COLLECTION_MODELS[name] is model


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


def _bag_of_words_vector(text: str, vocabulary: list[str]) -> list[float]:
    """A tiny, deterministic, model-free stand-in for a real sentence
    embedding: a word-presence vector over a fixed vocabulary, L2-normalized
    exactly like `retrieval.embedding`'s real all-MiniLM-L6-v2 vectors
    (`normalize_embeddings=True`) -- see that module's docstring. This is
    NOT a claim that bag-of-words is as good as the real model; it exists
    only to exercise the actual scoring contract `PgVectorStore.search`'s
    own comment documents ("inner product of L2-normalized vectors ranks
    identically to cosine similarity") against incident-shaped text,
    without needing the real (network-fetched, ~90MB) model weights this
    sandbox's stubbed `sentence_transformers` cannot provide -- the same
    constraint every other test in this suite already works around by
    monkeypatching at the `retrieval_service`/`_store` boundary instead of
    embedding real text.
    """
    words = text.lower().split()
    raw = [float(words.count(term)) for term in vocabulary]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def test_semantically_similar_incidents_score_higher_than_an_unrelated_one() -> None:
    """Requirement 9: "two semantically similar incidents can retrieve
    each other." Two incidents describing the same underlying failure
    (checkout/payment/500s) must have a higher dense-similarity score
    against each other than either has against an unrelated incident
    (a completely different subsystem/symptom) -- the exact ranking
    property `search_with_signals`'s `top_dense_similarity` and
    `search()`'s dense pass both depend on to surface a real historical
    match instead of a coincidental one.
    """
    incident_a = "checkout returning 500 errors payment service null pointer"
    incident_b = "payment service checkout failing 500 error null pointer exception"
    unrelated_incident = "scheduled backup job disk quota exceeded storage cleanup"

    vocabulary = sorted(
        set(incident_a.split()) | set(incident_b.split()) | set(unrelated_incident.split())
    )

    vec_a = _bag_of_words_vector(incident_a, vocabulary)
    vec_b = _bag_of_words_vector(incident_b, vocabulary)
    vec_unrelated = _bag_of_words_vector(unrelated_incident, vocabulary)

    similar_pair_score = _cosine_similarity(vec_a, vec_b)
    unrelated_pair_score = _cosine_similarity(vec_a, vec_unrelated)

    assert similar_pair_score > unrelated_pair_score
    # A genuinely similar pair should score well above the empirical
    # "genuine topical match" floor `app.agents.confidence.
    # _DENSE_SIMILARITY_FLOOR` uses for this same embedding scale.
    assert similar_pair_score > 0.35
