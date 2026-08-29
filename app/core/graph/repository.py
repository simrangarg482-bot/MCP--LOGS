"""Persistence for core/graph -- pure data access on `knowledge_graph_edges`.

Owned by: core/graph. What this module does NOT do is decide whether a
caller may see either endpoint of an edge -- that decision needs the source
entity tables (incidents, documents, ...) this module has no reason to know
about, and lives in `service.py`, which resolves both endpoints live before
ever returning an edge to a caller. This module's only filter is structural:
organization and `status='active'`, the same two columns `core.memory.
repository._visibility_clause` starts from.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.graph_models import KnowledgeGraphEdge

_ACTIVE = "active"
_REMOVED = "removed"


def _touches_entity(entity_type: str, entity_id: uuid.UUID):
    """Match an edge where `(entity_type, entity_id)` is EITHER endpoint.

    Shared by every "what's connected to this entity" query so a caller
    reading from the source side and one reading from the target side never
    diverge on what counts as a match.
    """
    return or_(
        and_(
            KnowledgeGraphEdge.source_entity_type == entity_type,
            KnowledgeGraphEdge.source_entity_id == entity_id,
        ),
        and_(
            KnowledgeGraphEdge.target_entity_type == entity_type,
            KnowledgeGraphEdge.target_entity_id == entity_id,
        ),
    )


async def get_direct_edges(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
) -> Sequence[KnowledgeGraphEdge]:
    """Every active stored edge touching `(entity_type, entity_id)`, in
    either direction.

    Tenant-scoped but NOT authorization-scoped: the row returned may name an
    entity the caller cannot see (the other endpoint). `service.py` resolves
    and authorizes both endpoints before this is ever handed back to a
    caller -- see this module's docstring.
    """
    stmt = select(KnowledgeGraphEdge).where(
        KnowledgeGraphEdge.organization_id == organization_id,
        KnowledgeGraphEdge.status == _ACTIVE,
        _touches_entity(entity_type, entity_id),
    )
    return (await session.execute(stmt)).scalars().all()


async def list_active_edges_by_relationship_type(
    session: AsyncSession, *, organization_id: uuid.UUID, relationship_type: str
) -> Sequence[KnowledgeGraphEdge]:
    """Every active edge of one relationship type, for a discovery/cleanup
    pass to walk (`service.discover_document_incident_edges`).
    """
    stmt = select(KnowledgeGraphEdge).where(
        KnowledgeGraphEdge.organization_id == organization_id,
        KnowledgeGraphEdge.status == _ACTIVE,
        KnowledgeGraphEdge.relationship_type == relationship_type,
    )
    return (await session.execute(stmt)).scalars().all()


async def upsert_derived_edge(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID | None,
    source_entity_type: str,
    source_entity_id: uuid.UUID,
    relationship_type: str,
    target_entity_type: str,
    target_entity_id: uuid.UUID,
    provenance_type: str,
    provenance_id: uuid.UUID | None,
    created_by: str,
    edge_metadata: dict | None = None,
) -> tuple[KnowledgeGraphEdge, str]:
    """Create, revive, or leave unchanged the one edge with this logical
    identity, returning `(row, action)` where `action` is `"created"`,
    `"revived"`, or `"unchanged"`.

    The caller (`service.py`) is responsible for having already applied
    `contract.canonical_direction` to `source_entity_id`/`target_entity_id`
    when the relationship is symmetric -- this function stores exactly the
    direction it is given, matching the unique constraint on
    `(organization_id, source_entity_type, source_entity_id,
    relationship_type, target_entity_type, target_entity_id)`.

    A second discovery of the same fact converges on the same row rather
    than accumulating a duplicate (queried directly by logical identity
    rather than relying on a database-level upsert, since a soft-deleted row
    that matches must be revived -- an `INSERT ... ON CONFLICT DO NOTHING`
    would not do that).
    """
    stmt = select(KnowledgeGraphEdge).where(
        KnowledgeGraphEdge.organization_id == organization_id,
        KnowledgeGraphEdge.source_entity_type == source_entity_type,
        KnowledgeGraphEdge.source_entity_id == source_entity_id,
        KnowledgeGraphEdge.relationship_type == relationship_type,
        KnowledgeGraphEdge.target_entity_type == target_entity_type,
        KnowledgeGraphEdge.target_entity_id == target_entity_id,
    )
    existing = (await session.execute(stmt)).scalars().first()

    if existing is None:
        row = KnowledgeGraphEdge(
            organization_id=organization_id,
            project_id=project_id,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            relationship_type=relationship_type,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            provenance_type=provenance_type,
            provenance_id=provenance_id,
            status=_ACTIVE,
            created_by=created_by,
            edge_metadata=edge_metadata,
        )
        session.add(row)
        await session.flush()
        return row, "created"

    if existing.status != _ACTIVE:
        existing.status = _ACTIVE
        existing.project_id = project_id
        existing.provenance_type = provenance_type
        existing.provenance_id = provenance_id
        existing.created_by = created_by
        existing.edge_metadata = edge_metadata
        await session.flush()
        return existing, "revived"

    return existing, "unchanged"


async def deactivate_edge(session: AsyncSession, edge_id: uuid.UUID) -> int:
    """Tombstone one edge. Idempotent: a second call matches zero rows."""
    stmt = (
        update(KnowledgeGraphEdge)
        .where(KnowledgeGraphEdge.id == edge_id, KnowledgeGraphEdge.status == _ACTIVE)
        .values(status=_REMOVED)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def deactivate_edges_touching_entity(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
) -> int:
    """Tombstone every active edge naming `(entity_type, entity_id)` as
    either endpoint.

    The physical half of lifecycle integration: called when a source
    document/incident is confirmed gone, so a stale edge does not merely
    fail the query-time visibility check (`service._resolve_entity`
    returning `None`) but stops existing in storage at all -- the same two-
    barrier discipline `core.memory.repository.soft_delete` documents for
    the Priority 3 bug this design is built to not repeat.
    """
    stmt = (
        update(KnowledgeGraphEdge)
        .where(
            KnowledgeGraphEdge.organization_id == organization_id,
            KnowledgeGraphEdge.status == _ACTIVE,
            _touches_entity(entity_type, entity_id),
        )
        .values(status=_REMOVED)
    )
    return int((await session.execute(stmt)).rowcount or 0)
