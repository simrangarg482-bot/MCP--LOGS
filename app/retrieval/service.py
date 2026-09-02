"""Public facade for retrieval/ (PROJECT_PLAN.md section 9.9): `search(query,
filters, top_k)`, `upsert(chunks)`, `delete(...)`.

Owned by: retrieval/. This is the only entry point other modules (ingestion,
eventually agents) should call into -- `retrieval.pgvector.store`,
`retrieval.embedding`, and `retrieval.ranking.fusion` are internal wiring
this file owns, not called directly by callers outside retrieval/.

`search()` runs hybrid retrieval (section 5.2) across *every* collection by
default, not one collection at a time: section 9.9's own literal signature
-- `search(query, filters, top_k)` -- takes no `collection` parameter, and
section 6.1's Retrieval Agent wants "ranked, authorization-filtered
evidence" without needing to already know which collection an answer will
come from; `ScoredChunk.collection` on each result still identifies it
after the fact. For each collection searched, both `PgVectorStore.search`
(dense) and `.lexical_search` (lexical/keyword) run against the same
tenant/project/ACL-filtered candidate set (the filters are hard `WHERE`
constraints inside those methods themselves, per section 5.4-5.5 -- never a
post-filter applied here), and every resulting list is fused into one ranked
list via `ranking.fusion.reciprocal_rank_fusion`.

The optional `collection` parameter (added for the Investigation Agent,
PROJECT_PLAN.md section 6.4) restricts the search to exactly one collection
instead of all three -- a real, later-emerging caller need section 9.9's own
signature didn't anticipate: the Investigation Agent's evidence-gathering
sub-stage needs to search "code" and "conversations" as two *separate*,
source-labeled steps (recent commits vs. Slack conversations), not one
mixed, fused result it would then have to guess the origin of.
`collection=None` (the default) preserves the original all-collections
behavior every existing caller (the Retrieval Agent) already relies on --
this is an additive, backward-compatible parameter, not a signature change
to the existing behavior.

The optional `include_metadata` parameter (also added for the Investigation
Agent, alongside the GitHub connector's extension to commits/PRs/issues)
opts into populating each `ScoredChunk.metadata` from its document's
`document_metadata` rows -- see `ScoredChunk.metadata`'s own docstring for
why this is opt-in rather than always joined. `False` (the default)
preserves every existing caller's query cost unchanged.

`upsert()` embeds every chunk's content once (`embedding.embed_texts`, a
single batched model call regardless of how many chunks or collections are
involved), groups the chunks by each chunk's own `collection` (an
`UpsertChunk` already names its target collection -- see that schema's
docstring), and issues one `PgVectorStore.upsert` call per collection group.

Reranking (section 5.3, cross-encoder) is explicitly out of scope: Milestone
5's own bullet list scopes this work to dense+BM25+RRF only; reranking is
flagged as the Retrieval Agent's future responsibility (section 6.1's own
"(3) cross-encoder reranking" step), not retrieval/'s.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval import embedding
from app.retrieval.pgvector.store import PgVectorStore
from app.retrieval.ranking.fusion import reciprocal_rank_fusion
from app.retrieval.schemas import (
    CollectionName,
    HybridSearchResult,
    ScoredChunk,
    SearchFilters,
    UpsertChunk,
)

# Every collection this milestone supports (`app.database.models.
# retrieval_models`'s module docstring: no "incidents" collection yet).
_ALL_COLLECTIONS: tuple[CollectionName, ...] = ("documentation", "code", "conversations")

# Stateless -- see `PgVectorStore`'s own docstring on why one shared instance
# safely serves every collection and every caller's session.
_store = PgVectorStore()


async def search(
    session: AsyncSession,
    query: str,
    filters: SearchFilters,
    top_k: int,
    collection: CollectionName | None = None,
    *,
    include_metadata: bool = False,
) -> list[ScoredChunk]:
    """Hybrid search across every collection, or just `collection` if given
    -- see module docstring.

    Returns at most `top_k` chunks overall (not `top_k` per collection),
    ranked by fused RRF score descending.
    """
    collections_to_search = _ALL_COLLECTIONS if collection is None else (collection,)

    query_embedding = await embedding.embed_query(query)

    if collection is None and not include_metadata:
        dense = await _store.search_all(session, query_embedding, filters, top_k)
        lexical = await _store.lexical_search_all(session, query, filters, top_k)
        return reciprocal_rank_fusion([dense, lexical], top_k=top_k)

    result_lists: list[list[ScoredChunk]] = []
    for collection_name in collections_to_search:
        result_lists.append(
            await _store.search(
                session,
                collection_name,
                query_embedding,
                filters,
                top_k,
                include_metadata=include_metadata,
            )
        )
        result_lists.append(
            await _store.lexical_search(
                session,
                collection_name,
                query,
                filters,
                top_k,
                include_metadata=include_metadata,
            )
        )

    return reciprocal_rank_fusion(result_lists, top_k=top_k)


async def search_with_signals(
    session: AsyncSession,
    query: str,
    filters: SearchFilters,
    top_k: int,
) -> HybridSearchResult:
    """`search()` over every collection (no metadata join), but also
    returning the pre-fusion retrieval-quality signals `app.agents.
    confidence` reads -- see `HybridSearchResult`'s docstring for why this
    exists as a separate entry point rather than a richer `search()` return.

    Same query cost as `search()`'s all-collections fast path: it runs the
    identical `search_all` (dense) + `lexical_search_all` (lexical) passes
    and the same `reciprocal_rank_fusion`, and simply keeps the top dense
    score that fusion would otherwise discard. Only the Retrieval Agent
    (`app.agents.retrieval.node`) calls this; every other caller
    (ingestion, the Investigation Agent's collection-scoped steps) still
    uses `search()`.
    """
    query_embedding = await embedding.embed_query(query)
    dense = await _store.search_all(session, query_embedding, filters, top_k)
    lexical = await _store.lexical_search_all(session, query, filters, top_k)
    fused = reciprocal_rank_fusion([dense, lexical], top_k=top_k)
    return HybridSearchResult(
        chunks=fused,
        # `dense` is ordered by similarity descending (`search_all` ends with
        # `ORDER BY distance ASC` over the negated inner product), so [0] is
        # the single best dense hit. Its `score` is already negated back to a
        # normal cosine similarity by `PgVectorStore`.
        top_dense_similarity=dense[0].score if dense else None,
    )


async def upsert(session: AsyncSession, chunks: list[UpsertChunk]) -> None:
    """Embed and store `chunks`, grouped by each chunk's own `collection`.

    `chunks` carry raw content, not a precomputed embedding (`UpsertChunk`'s
    docstring) -- embedding happens here, once per call, batched across
    every chunk regardless of which collection it targets, matching section
    9.9's `upsert(chunks)` signature (no separate embed step exposed to
    callers).
    """
    if not chunks:
        return

    embeddings = await embedding.embed_texts([chunk.content for chunk in chunks])

    by_collection: dict[CollectionName, list[tuple[UpsertChunk, list[float]]]] = defaultdict(list)
    for chunk, chunk_embedding in zip(chunks, embeddings, strict=True):
        by_collection[chunk.collection].append((chunk, chunk_embedding))

    for collection, pairs in by_collection.items():
        collection_chunks = [pair[0] for pair in pairs]
        collection_embeddings = [pair[1] for pair in pairs]
        await _store.upsert(session, collection, collection_chunks, collection_embeddings)


async def delete(session: AsyncSession, collection: CollectionName, document_id: uuid.UUID) -> None:
    """Remove every chunk belonging to `document_id` from `collection`."""
    await _store.delete(session, collection, document_id)
