"""The memory-recall "system under test" seam.

`MemoryAdapter` is a `typing.Protocol`, same structural-typing convention as
`RetrievalAdapter`/`SimilarityScorer`.

`FixtureMemoryAdapter` (Mode 1) mirrors `core.memory.repository`'s real
visibility semantics against the in-memory corpus:

    organization + status='active'
    AND ( user-scoped AND owned by this actor
          OR project-scoped AND in a project this actor may see )

then ranks the survivors by cosine distance. **Filter first, then rank** --
deliberately the same order as the production SQL, because a fixture adapter
that filtered after ranking would let the evaluation pass while the real
system leaked, which is worse than having no evaluation at all.

`RealMemoryAdapter` (Mode 2/3) wraps the actual
`core.memory.service.recall_relevant`. Code-complete and not exercised
end-to-end here for the same reason as every other `Real*Adapter`: no live
database in this environment.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.fixtures.memory_corpus import MemoryFixture, fake_embed
from app.evaluation.schemas import EvaluationCase


@runtime_checkable
class MemoryAdapter(Protocol):
    async def recall(self, case: EvaluationCase, limit: int) -> list[str]:
        """Return the LABELS of memories recalled for `case.query`, closest
        first, with `case.identity`'s visibility applied."""
        ...


def _cosine_distance(a: list[float], b: list[float]) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b, strict=True))


class FixtureMemoryAdapter:
    def __init__(self, corpus: list[MemoryFixture], *, relevance_threshold: float = 0.35) -> None:
        self._corpus = corpus
        self._threshold = relevance_threshold

    def _visible(self, fixture: MemoryFixture, case: EvaluationCase) -> bool:
        if fixture.status != "active":
            return False
        if fixture.scope == "user":
            return fixture.owner_user_id == case.identity.user_id
        if fixture.scope == "project":
            allowed = {str(pid) for pid in case.identity.project_permissions}
            return fixture.project_id is not None and fixture.project_id in allowed
        return False  # unknown scope fails closed, as in production

    async def recall(self, case: EvaluationCase, limit: int) -> list[str]:
        query_vector = fake_embed(case.query)
        visible = [f for f in self._corpus if self._visible(f, case)]
        scored = [(f, _cosine_distance(query_vector, f.vector())) for f in visible]
        scored.sort(key=lambda pair: pair[1])
        return [
            fixture.label
            for fixture, distance in scored[:limit]
            if (1.0 - distance) >= self._threshold
        ]


class RealMemoryAdapter:
    """Mode 2/3: the real `core.memory.service.recall_relevant`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recall(self, case: EvaluationCase, limit: int) -> list[str]:
        from app.core.memory import service as memory_service

        actor = case.identity.to_identity(case.organization_uuid)
        recalled = await memory_service.recall_relevant(self._session, actor, case.query)
        # Real memories have UUIDs, not labels -- a dataset run against real
        # data would need its own id mapping. Returned as strings so the
        # runner's comparison logic is identical either way.
        return [str(m.id) for m in recalled[:limit]]
