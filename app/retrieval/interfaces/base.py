"""The `VectorStore` protocol every backend (pgvector today, Qdrant later)
implements, per PROJECT_PLAN.md section 8.4: "Both backends sit behind one
`retrieval.VectorStore` interface (`search`, `upsert`, `delete`) ... a
configuration decision per collection, not an architectural fork requiring
different code paths."

Modeled as a `typing.Protocol`, not a base class -- same composition-over-
inheritance rationale as ingestion's `Connector` protocol
(`app.ingestion.connectors.base`): a backend implementation can't
accidentally inherit behavior it shouldn't own.

Milestone 5 builds only the pgvector backend (PROJECT_PLAN.md's own
Milestone 5 bullet list names "pgvector backend" explicitly and never
mentions Qdrant) -- `app/retrieval/qdrant/` stays an empty placeholder
package until a real per-collection need for it materializes (section 8.3:
Qdrant "wins at larger scale or when payload-filtered ANN search throughput
matters more than transactional co-location" -- not yet true for anything
this project has built).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.schemas import CollectionName, ScoredChunk, SearchFilters, UpsertChunk


@runtime_checkable
class VectorStore(Protocol):
    """One implementation per backend. A single instance may serve multiple
    collections -- `collection` is a routing parameter on every method, not
    something an instance is bound to at construction time. Which backend
    instance actually serves a given collection (today: pgvector for all
    three) is a wiring concern for `retrieval/service.py`, not this
    protocol.

    Every method takes `session: AsyncSession` as its first argument,
    matching the convention used everywhere else in this codebase (core/*,
    ingestion/*): the caller's session is passed in, never opened here, so
    a vector-store write can share the same transaction as whatever
    surrounding operation triggered it (e.g. ingestion's savepoint around
    persisting a `Document` row) rather than being a separate, independently
    committed side effect.

    Embedding is NOT this protocol's job: `retrieval/service.py` (the public
    facade, task #16) calls `retrieval.embedding.embed_texts()` once and
    passes the resulting vectors to `upsert` alongside the chunks they
    belong to. Keeping embedding out of `VectorStore` avoids every backend
    implementation (pgvector, eventually Qdrant) duplicating identical,
    backend-agnostic embedding logic.
    """

    async def search(
        self,
        session: AsyncSession,
        collection: CollectionName,
        query_embedding: list[float],
        filters: SearchFilters,
        top_k: int,
        *,
        include_metadata: bool = False,
    ) -> list[ScoredChunk]:
        """Dense (embedding-similarity) search within `collection`, with
        `filters` applied as hard constraints on the query itself
        (PROJECT_PLAN.md sections 5.4-5.5 -- never as a post-filter on
        already-returned results). Returns at most `top_k` chunks, ordered
        by descending relevance.

        `include_metadata` (default `False`) opts into populating each
        returned `ScoredChunk.metadata` from that chunk's document's
        `document_metadata` rows -- added for the Investigation Agent's
        evidence-gathering step, which needs to tell a GitHub file apart
        from a commit/PR/issue chunk (see `ScoredChunk.metadata`'s
        docstring). Left off by default since the higher-volume Answer
        Agent path never needs it and an unconditional join would be a real,
        unnecessary cost there.
        """
        ...

    async def lexical_search(
        self,
        session: AsyncSession,
        collection: CollectionName,
        query_text: str,
        filters: SearchFilters,
        top_k: int,
        *,
        include_metadata: bool = False,
    ) -> list[ScoredChunk]:
        """Lexical/keyword search within `collection` -- the other half of
        hybrid search (PROJECT_PLAN.md section 5.2), fused with `search`'s
        dense results via reciprocal rank fusion in
        `retrieval/ranking/fusion.py` (task #16). Same hard-filter
        requirement as `search`. A separate protocol method, not a mode
        flag on `search`, because a backend's lexical mechanism can differ
        entirely from its dense one (pgvector: Postgres full-text search;
        Qdrant, if ever added here: its own text index), the same reason
        `search`/`upsert`/`delete` are already separate methods rather than
        one method with a `mode` parameter.

        `include_metadata` -- same contract as `search()`'s.
        """
        ...

    async def upsert(
        self,
        session: AsyncSession,
        collection: CollectionName,
        chunks: list[UpsertChunk],
        embeddings: list[list[float]],
    ) -> None:
        """Store `chunks` (with `embeddings[i]` corresponding to
        `chunks[i]`) in `collection`, keyed by `(document_id, chunk_index)`
        -- re-upserting an existing key replaces its prior content/
        embedding, matching `documents`' own version-on-change semantics
        one layer up (a new document version upserts fresh chunks; it does
        not need to delete the old ones first, since the old document_id's
        chunks stay tied to the old, superseded `Document` row).

        `len(chunks) == len(embeddings)` is a precondition the caller
        guarantees, not something this method re-validates.
        """
        ...

    async def delete(
        self, session: AsyncSession, collection: CollectionName, document_id: uuid.UUID
    ) -> None:
        """Remove every chunk belonging to `document_id` from `collection`."""
        ...
