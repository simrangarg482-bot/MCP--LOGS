"""Knowledge graph router -- read-only traversal plus the one assertable
manual relationship (Priority 5).

Owned by: app/api. Thin pass-through to `app.core.graph.service` -- no
business logic beyond request/response translation, the same convention
every router in this package follows (see `memory.py`'s identical note).

WHY THE SURFACE IS THIS NARROW
    Three endpoints, no query language, no arbitrary depth. `GET /related`
    is the only traversal endpoint, and its `depth` parameter can only ever
    NARROW `core.graph.schemas.MAX_TRAVERSAL_DEPTH` --
    `core.graph.service.get_neighborhood` clamps it server-side, so there is
    no request shape that reaches deeper than the hard ceiling regardless of
    what a caller sends. There is deliberately no `POST /graph/query` or
    equivalent: `core.graph.contract` is the only vocabulary a caller can
    traverse, not an open-ended expression language.

AUTHORIZATION IS STRUCTURAL, NOT PARAMETERIC
    No endpoint accepts an `organization_id`. Tenancy comes entirely from
    the authenticated `Identity` (`actor.organization_id`), and every entity
    a response could ever mention -- the origin, every node, both endpoints
    of every relationship -- is independently re-authorized by
    `core.graph.service._resolve_entity` using each entity type's own
    existing read gate. See that module's docstring for the full invariant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentIdentity, DbSession
from app.core.graph import service as graph_service
from app.core.graph.contract import EntityType
from app.core.graph.schemas import (
    MAX_TRAVERSAL_DEPTH,
    GraphNeighborhood,
    GraphRelationship,
    RelationshipCreate,
)

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])


@router.get(
    "/entities/{entity_type}/{entity_id}/relationships", response_model=list[GraphRelationship]
)
async def get_direct_relationships(
    entity_type: EntityType, entity_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[GraphRelationship]:
    """What is directly connected to one entity (depth 1 only).

    A relationship is never itself citable evidence -- see
    `docs/KNOWLEDGE_GRAPH.md`. `entity_type` not found (or not visible to
    this caller) reports as `NotFoundError`, matching every other
    single-resource `GET` in this codebase.
    """
    return await graph_service.get_direct_relationships(session, actor, entity_type, entity_id)


@router.get("/entities/{entity_type}/{entity_id}/related", response_model=GraphNeighborhood)
async def get_related_entities(
    entity_type: EntityType,
    entity_id: uuid.UUID,
    actor: CurrentIdentity,
    session: DbSession,
    depth: int = Query(default=MAX_TRAVERSAL_DEPTH, ge=1, le=MAX_TRAVERSAL_DEPTH),
) -> GraphNeighborhood:
    """Bounded, authorized traversal from one entity.

    `depth` can only narrow `MAX_TRAVERSAL_DEPTH` -- the `le` bound above is
    enforced by FastAPI/Pydantic before this ever reaches the service, and
    `get_neighborhood` clamps it again regardless, so there is no path
    through which a caller reaches more hops than the ceiling allows.
    `truncated=true` on the response means the node or edge cap was hit
    before the walk exhausted what is reachable -- an honest signal, not a
    silently partial result.
    """
    return await graph_service.get_neighborhood(
        session, actor, entity_type, entity_id, max_depth=depth
    )


@router.post(
    "/relationships", response_model=GraphRelationship, status_code=status.HTTP_201_CREATED
)
async def create_manual_relationship(
    data: RelationshipCreate, actor: CurrentIdentity, session: DbSession
) -> GraphRelationship:
    """Assert a manual relationship -- today, only `incident related_to
    incident`.

    Requires write access to BOTH incidents, not just one (see
    `core.graph.service.create_manual_relationship`'s docstring). Asserting
    the same relationship a second time, in either direction, converges on
    the same stored edge rather than creating a duplicate.
    """
    return await graph_service.create_manual_relationship(session, actor, data)
