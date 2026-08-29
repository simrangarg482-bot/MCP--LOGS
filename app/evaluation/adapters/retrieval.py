"""The retrieval "system under test" seam.

`RetrievalAdapter` is a `typing.Protocol` (same structural-typing convention
as `SimilarityScorer` -- see that module's docstring). Two implementations:

- `FixtureRetrievalAdapter` -- Mode 1 (deterministic). Serves `ScoredChunk`s
  from an in-memory fixture corpus (`app.evaluation.fixtures.corpus`),
  ranked by the same cheap lexical-overlap heuristic
  `TokenOverlapSimilarityScorer` uses elsewhere in this package, applying
  `SearchFilters`' actual permission/project semantics via `Identity.
  has_permission` -- so permission-aware retrieval cases (see package
  README) are genuinely exercised, not stubbed past.
- `RealRetrievalAdapter` -- Mode 2/3. A thin wrapper around the real
  `app.retrieval.service.search`, for running the exact same dataset
  against a real Postgres+pgvector instance once one is available in a
  given environment. Code-complete and untested end-to-end in this
  repository's own development environment for the same reason
  `docs/PROJECT_STATUS.md` documents for `scripts/rls_isolation_test.py`:
  no disposable Postgres+pgvector instance exists here to run it against.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.schemas import EvaluationCase, normalize_text
from app.retrieval.schemas import ScoredChunk, SearchFilters


@runtime_checkable
class RetrievalAdapter(Protocol):
    async def search(self, case: EvaluationCase, top_k: int) -> list[ScoredChunk]:
        """Return up to `top_k` `ScoredChunk`s for `case.query`, already
        ranked (index 0 = most relevant), with `case.identity`'s permissions
        applied as a hard filter -- matching `retrieval.service.search`'s
        own contract."""
        ...


def _visible(chunk: ScoredChunk, case: EvaluationCase) -> bool:
    """Mirrors `SearchFilters.permission_codes`' real semantics
    (`ENGINEERING_DECISIONS.md` #007, restated in `retrieval.schemas.
    SearchFilters`'s own docstring): a chunk with no ACL code is
    unrestricted; one with a code requires that code to be held by the
    caller. `ScoredChunk` itself carries no `acl_permission_code` field
    (that lives only on the stored row / `UpsertChunk`), so the fixture
    corpus (`app.evaluation.fixtures.corpus`) attaches it via `metadata`
    instead -- the same "opt-in `metadata` dict" extension point
    `ScoredChunk.metadata` already documents for the Investigation Agent's
    own needs.
    """
    acl_code = chunk.metadata.get("acl_permission_code")
    if not acl_code:
        return True
    return acl_code in case.identity.permissions


#: Word-boundary tokenizer for ranking purposes only -- deliberately
#: different from `normalize_text`'s "lowercase + collapse whitespace, keep
#: punctuation" used by assertions/grounding substring checks (which must
#: preserve exact text). A ranking heuristic has no such requirement, and
#: keeping punctuation attached to a word here is actively harmful: e.g. a
#: query ending "...deployment 456?" and a chunk containing "...456 changed"
#: would never overlap on "456" at all if punctuation weren't stripped
#: first, silently under-scoring an otherwise clearly relevant chunk.
_WORD_PATTERN = re.compile(r"\w+")

#: A small stopword list, filtered out before scoring -- without this, two
#: documents sharing only common function words ("the", "service", "after")
#: can out-score a document sharing the query's actual distinguishing
#: content words, which defeats the point of a curated fixture corpus. This
#: also makes the fixture adapter more faithful to the real system it
#: stands in for: `retrieval.service`'s lexical half of hybrid search runs
#: on Postgres full-text search, whose `tsvector` conversion is itself
#: stopword-aware for the same reason.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "was", "were", "are", "be", "been", "being",
        "did", "do", "does", "after", "before", "in", "on", "at", "to", "of",
        "and", "or", "for", "with", "by", "from", "this", "that", "it", "as",
        "what", "why", "how", "when", "which", "who",
    }
)


def _tokenize_for_ranking(text: str) -> set[str]:
    words = _WORD_PATTERN.findall(normalize_text(text))
    return {word for word in words if word not in _STOPWORDS}


class FixtureRetrievalAdapter:
    """Deterministic Mode 1 retrieval: ranks a fixed in-memory corpus by
    word-overlap between `case.query` and each chunk's content plus title --
    crude by design (see `TokenOverlapSimilarityScorer`'s docstring for the
    same "deterministic fallback, not a real ranking model" reasoning), good
    enough to make retrieval fixtures behave predictably without requiring
    an embedding model or a database.
    """

    def __init__(self, corpus: list[ScoredChunk]) -> None:
        self._corpus = corpus

    async def search(self, case: EvaluationCase, top_k: int) -> list[ScoredChunk]:
        query_words = _tokenize_for_ranking(case.query)
        visible = [chunk for chunk in self._corpus if _visible(chunk, case)]

        def _overlap_score(chunk: ScoredChunk) -> float:
            chunk_words = _tokenize_for_ranking(f"{chunk.title or ''} {chunk.content}")
            if not query_words or not chunk_words:
                return 0.0
            return len(query_words & chunk_words) / len(query_words | chunk_words)

        scored = sorted(visible, key=_overlap_score, reverse=True)
        # Re-attach the computed score (the corpus's own placeholder `score`
        # is irrelevant here -- ranking must reflect this adapter's own
        # query-dependent computation, the same way a real `VectorStore`
        # would return a query-dependent score, not a fixed one).
        rescored = [chunk.model_copy(update={"score": _overlap_score(chunk)}) for chunk in scored]
        return [chunk for chunk in rescored if chunk.score > 0.0][:top_k]


class RealRetrievalAdapter:
    """Mode 2/3: wraps the real, unmodified `app.retrieval.service.search`.
    See module docstring for this codebase's own convention on labeling
    infrastructure-dependent code "code-complete, not yet run here."
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(self, case: EvaluationCase, top_k: int) -> list[ScoredChunk]:
        from app.retrieval import service as retrieval_service

        filters = SearchFilters(
            organization_id=case.organization_uuid,
            permission_codes=case.identity.permissions,
        )
        return await retrieval_service.search(
            self._session, case.query, filters, top_k, include_metadata=True
        )
