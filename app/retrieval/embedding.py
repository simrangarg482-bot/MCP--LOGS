"""Embedding generation for retrieval/ (PROJECT_PLAN.md section 9.9's
"Embedding generation integration" bullet, Milestone 5).

Owned by: retrieval/. Wraps `sentence-transformers` (already a pinned core
dependency, `pyproject.toml`) around the model pinned in
ENGINEERING_DECISIONS.md #006 (`all-MiniLM-L6-v2`, 384 dimensions).

`EMBEDDING_DIMENSION` duplicates `app.database.models.retrieval_models`'s
`_EMBEDDING_DIMENSION` rather than one module importing the other's
constant: `database/` is a leaf module (import-linter's "database is a leaf
module" contract forbids it depending on anything, including retrieval/),
so the two constants can't be unified without violating that boundary. Both
are ENGINEERING_DECISIONS.md #006's single source of truth; keep them in
sync by hand if the model ever changes.

`SentenceTransformer.encode()` is a synchronous, CPU-bound call -- run via
`asyncio.to_thread` so it doesn't block the event loop the rest of this
async-throughout codebase relies on. This matters most for the ingestion
worker, which processes many chunks per job; blocking the loop on each one
would serialize embedding generation behind everything else scheduled on it.

Embeddings are L2-normalized at generation time (`normalize_embeddings=True`):
with unit-normalized vectors, cosine similarity and inner product become the
same computation, letting `retrieval/pgvector`'s search (task #15) use
pgvector's inner-product operator (`<#>`), which pgvector's own docs note is
cheaper than computing cosine distance (`<=>`) directly, for an identical
ranking result.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from sentence_transformers import SentenceTransformer

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # ENGINEERING_DECISIONS.md #006

#: Must match `app.database.models.retrieval_models._EMBEDDING_DIMENSION`
#: exactly -- a mismatch would make `VectorStore.upsert`/`search` produce
#: vectors of the wrong width for the `vector` Postgres extension's fixed-
#: width columns, which Postgres rejects at insert time, not silently.
EMBEDDING_DIMENSION = 384


@lru_cache
def _get_model() -> SentenceTransformer:
    """Load the embedding model once per process and cache it.

    Loading is itself slow (reads/downloads model weights); `lru_cache` with
    no arguments makes this a load-once-per-process singleton, matching
    `get_settings()`'s existing cached-accessor pattern in this codebase.
    """
    return SentenceTransformer(_MODEL_NAME)


async def embed_query(query: str) -> list[float]:
    """Embed a single search query string."""
    model = _get_model()
    vector = await asyncio.to_thread(model.encode, query, normalize_embeddings=True)
    return vector.tolist()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of chunk contents in one model call.

    Batched rather than one `embed_query`-style call per chunk:
    `sentence-transformers`' `encode()` batches internally far more
    efficiently than looping over single-text calls at the Python level,
    which matters here since `VectorStore.upsert` may be handed dozens of
    chunks from a single document at once.
    """
    if not texts:
        return []
    model = _get_model()
    vectors = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)
    return vectors.tolist()
