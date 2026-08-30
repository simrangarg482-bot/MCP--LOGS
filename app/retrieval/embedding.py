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
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from functools import partial

from sentence_transformers import SentenceTransformer
from opentelemetry import trace

from app.shared.config.settings import get_settings

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # ENGINEERING_DECISIONS.md #006
tracer = trace.get_tracer(__name__)

# Bounds the CPU-bound `encode()` call (and, on first use per process, the
# model-weights download it triggers via `_get_model`) so a stall here
# surfaces as a clear timeout instead of hanging the ingestion job
# indefinitely -- `asyncio.to_thread` alone has no timeout of its own, and
# this call runs while the caller's DB transaction/savepoint is still open,
# so an unbounded hang here holds that transaction open unboundedly too.
_ENCODE_TIMEOUT_SECONDS = 120.0

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


@lru_cache
def _get_embedding_executor() -> ThreadPoolExecutor:
    """Dedicated bounded executor so timed-out encodes cannot multiply in
    asyncio's shared, comparatively large default executor.
    """
    return ThreadPoolExecutor(
        max_workers=get_settings().embedding_worker_threads,
        thread_name_prefix="ekip-embedding",
    )


async def _encode(value: str | list[str]):
    model = _get_model()
    loop = asyncio.get_running_loop()
    call = partial(
        model.encode,
        value,
        normalize_embeddings=True,
        batch_size=get_settings().embedding_batch_size,
    )
    item_count = len(value) if isinstance(value, list) else 1
    with tracer.start_as_current_span("retrieval.embed") as span:
        span.set_attribute("embedding.model", _MODEL_NAME)
        span.set_attribute("embedding.item_count", item_count)
        return await asyncio.wait_for(
            loop.run_in_executor(_get_embedding_executor(), call),
            timeout=_ENCODE_TIMEOUT_SECONDS,
        )


async def embed_query(query: str) -> list[float]:
    """Embed a single search query string."""
    vector = await _encode(query)
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
    vectors = await _encode(texts)
    return vectors.tolist()
