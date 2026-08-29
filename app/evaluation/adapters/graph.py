"""The knowledge-graph traversal "system under test" seam.

`GraphAdapter` is a `typing.Protocol`, same structural-typing convention as
`RetrievalAdapter`/`MemoryAdapter`.

`FixtureGraphAdapter` (Mode 1) mirrors `core.graph.service`'s real
authorization rules against the in-memory fixture graph:

    organization matches
    AND NOT deleted
    AND ( incident/investigation: incident:read on its project
          OR project: same organization is enough
          OR postmortem: approved/published, else postmortem:write/approve
          OR document: published, else knowledge:review )

applied to every node BEFORE it can be returned, never as a post-filter --
the fixture equivalent of `_resolve_entity`'s "authorization is part of
resolution, not a filter" design (see that module's docstring). It reuses
the REAL `Identity.has_permission` rather than reimplementing permission
logic, since graph visibility (unlike memory's ownership/membership rules)
genuinely is permission-code-based -- there is nothing left to duplicate.

`RealGraphAdapter` (Mode 2/3) wraps the actual
`core.graph.service.get_neighborhood`. Code-complete and not exercised
end-to-end here for the same reason as every other `Real*Adapter`: no live
database in this environment. Real mode has no fixture labels to resolve
against, so `EvaluationCase.origin_label` is interpreted as
`"<entity_type>:<uuid>"` in that mode only.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.fixtures.graph_corpus import (
    ENTITIES_BY_LABEL,
    GRAPH_EDGES,
    GraphEdgeFixture,
    GraphEntityFixture,
)
from app.evaluation.schemas import EvaluationCase


@runtime_checkable
class GraphAdapter(Protocol):
    async def traverse(self, case: EvaluationCase, depth: int) -> list[str]:
        """Return the LABELS of entities reachable from `case.origin_label`
        within `depth` hops, with `case.identity`'s visibility applied.
        Excludes the origin itself."""
        ...


def _visible(entity: GraphEntityFixture, case: EvaluationCase) -> bool:
    if entity.organization_id != case.organization_id:
        return False
    if entity.deleted:
        return False

    identity = case.identity.to_identity(case.organization_uuid)
    project_uuid = uuid.UUID(entity.project_id) if entity.project_id else None

    if entity.entity_type in ("incident", "investigation"):
        return identity.has_permission("incident:read", project_id=project_uuid)
    if entity.entity_type == "project":
        return True  # same-organization check already passed above
    if entity.entity_type == "postmortem":
        if entity.status in ("approved", "published"):
            return True
        return identity.has_permission(
            "postmortem:write", project_id=project_uuid
        ) or identity.has_permission("postmortem:approve", project_id=project_uuid)
    if entity.entity_type == "document":
        if entity.status == "published":
            return True
        return identity.has_permission("knowledge:review", project_id=project_uuid)
    return False  # unknown entity type fails closed, as in production


def _neighbor_labels(label: str, edges: list[GraphEdgeFixture]):
    """Either endpoint reaches the other -- matching
    `core.graph.repository.get_direct_edges`, which matches an entity
    against both `source_entity_id` and `target_entity_id`."""
    for edge in edges:
        if edge.source_label == label:
            yield edge.target_label
        elif edge.target_label == label:
            yield edge.source_label


class FixtureGraphAdapter:
    def __init__(
        self,
        entities: dict[str, GraphEntityFixture] | None = None,
        edges: list[GraphEdgeFixture] | None = None,
    ) -> None:
        self._entities = entities if entities is not None else ENTITIES_BY_LABEL
        self._edges = edges if edges is not None else GRAPH_EDGES

    async def traverse(self, case: EvaluationCase, depth: int) -> list[str]:
        origin_label = case.origin_label
        if origin_label is None:
            return []
        origin = self._entities.get(origin_label)
        if origin is None or origin.organization_id != case.organization_id:
            return []

        visited = {origin_label}
        frontier = [origin_label]
        for _ in range(max(1, depth)):
            next_frontier: list[str] = []
            for label in frontier:
                for neighbor_label in _neighbor_labels(label, self._edges):
                    if neighbor_label in visited:
                        continue
                    neighbor = self._entities.get(neighbor_label)
                    if neighbor is None or not _visible(neighbor, case):
                        continue
                    visited.add(neighbor_label)
                    next_frontier.append(neighbor_label)
            frontier = next_frontier

        visited.discard(origin_label)
        return sorted(visited)


class RealGraphAdapter:
    """Mode 2/3: the real `core.graph.service.get_neighborhood`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def traverse(self, case: EvaluationCase, depth: int) -> list[str]:
        from app.core.graph import service as graph_service

        if case.origin_label is None or ":" not in case.origin_label:
            raise ValueError(
                "RealGraphAdapter requires origin_label as '<entity_type>:<uuid>' "
                f"in real mode; got {case.origin_label!r}"
            )
        entity_type, raw_entity_id = case.origin_label.split(":", 1)
        entity_id = uuid.UUID(raw_entity_id)
        actor = case.identity.to_identity(case.organization_uuid)
        neighborhood = await graph_service.get_neighborhood(
            self._session, actor, entity_type, entity_id, max_depth=depth
        )
        # Real nodes have UUIDs, not labels -- a dataset run against real
        # data would need its own id mapping. Returned as strings so the
        # runner's comparison logic is identical either way.
        return [str(node.entity_id) for node in neighborhood.nodes if node.entity_id != entity_id]
