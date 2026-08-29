"""Public interface for core/graph -- entity resolution, authorized
traversal, manual-edge creation, and the deterministic discovery pass.

Owned by: core/graph. Business rules and the authorization decisions live
here; SQL against `knowledge_graph_edges` lives in repository.py; SQL
against every OTHER entity's own table is read directly from that entity's
existing repository module (`core.incidents.repository`,
`core.knowledge.repository`, `core.tenancy.repository`) -- there is no
second copy of "what is a valid incident" anywhere in this module.

THE CENTRAL INVARIANT -- AUTHORIZATION IS PART OF RESOLUTION, NOT A FILTER
    Every entity this module ever returns to a caller -- the traversal
    origin, every node reached during expansion, both endpoints of every
    relationship -- passes through `_resolve_entity`, which re-fetches the
    row from its OWN source of truth and re-applies that entity type's own
    existing read gate (`incident:read`, the document published/`knowledge:
    review` rule, the postmortem approved/`postmortem:write`/`postmortem:
    approve` rule). An edge row in `knowledge_graph_edges` is never trusted
    on its own to mean "the caller may see this" -- it is a hint about what
    to look up, and the lookup is what decides visibility. This is what
    satisfies the spec's two invariants: a relationship never reveals an
    entity the caller could not otherwise access, and a deleted/invisible
    source or target cannot leak through a derived edge, because resolving
    it is exactly the step that would fail.

    No new permission code is introduced. Every check below reuses the
    permission string and the `Identity.has_permission`/`require_project_
    permission` mechanism the owning module (`core.incidents`, `core.
    knowledge`) already established for that entity type.

WHY THIS MODULE RESOLVES ENTITIES ITSELF RATHER THAN CALLING core.incidents.
service.get_incident (etc.)
    Those functions raise on a denial (`NotFoundError`/`PermissionDeniedError`)
    because a single "fetch this one thing" request is naturally an
    all-or-nothing question. Traversal is not: expanding a neighborhood must
    quietly DROP an unauthorized or gone node and keep going, not abort the
    whole call because one of potentially many reachable entities is
    invisible to this caller. So this module reads the same underlying rows
    through each domain's `repository` module and re-applies the same rule
    those `service` functions apply, returning `None` instead of raising.
    The rule itself is not duplicated logic in the sense of drifting
    independently -- it is copied from the one real source (`core.incidents.
    service.get_incident`/`get_postmortem`, `core.knowledge.service.
    get_document`) and must be kept in sync if those change; there is no
    mechanical way to share a `raise`-shaped check and a `None`-shaped one
    without either forcing exceptions into a hot traversal loop or forcing
    a "swallow and re-raise" wrapper that most of this codebase avoids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import NotFoundError, ValidationError
from app.core.graph import contract, repository
from app.core.graph.schemas import (
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    MAX_TRAVERSAL_DEPTH,
    DiscoveryResult,
    EntityRef,
    GraphNeighborhood,
    GraphRelationship,
    RelationshipCreate,
)
from app.core.incidents import repository as incidents_repository
from app.core.knowledge import repository as knowledge_repository
from app.core.tenancy import repository as tenancy_repository
from app.core.users.service import require_project_permission
from app.database.models.core_models import IncidentTimeline
from app.database.models.ingestion_models import DocumentMetadata
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_INCIDENT_READ_PERMISSION = "incident:read"
_INCIDENT_WRITE_PERMISSION = "incident:write"
_POSTMORTEM_WRITE_PERMISSION = "postmortem:write"
_POSTMORTEM_APPROVE_PERMISSION = "postmortem:approve"
_REVIEW_PERMISSION = "knowledge:review"

# Must match `core.knowledge.service._SOURCE_INCIDENT_METADATA_KEY` exactly
# -- not imported (that name is module-private), the same "duplicated
# string, documented, not shared" convention `EMBEDDING_DIMENSION` already
# uses across retrieval_models.py/retrieval/embedding.py/memory_models.py.
_SOURCE_INCIDENT_METADATA_KEY = "source_incident_id"

# A single incident is not expected to accumulate more than a handful of
# investigation runs; bounded anyway so one pathological incident cannot
# blow past the traversal's own edge cap before other relationships get a
# chance to appear.
_MAX_INVESTIGATION_FANOUT = 10

_PROVENANCE_ORDER = {"foreign_key": 0, "deterministic_extraction": 1, "manual": 2}


# --------------------------------------------------------------------------
# entity resolution -- see module docstring
# --------------------------------------------------------------------------


async def _resolve_incident(
    session: AsyncSession, actor: Identity, entity_id: uuid.UUID
) -> EntityRef | None:
    row = await incidents_repository.get_incident_by_id(session, entity_id)
    if row is None or row.organization_id != actor.organization_id:
        return None
    if not actor.has_permission(_INCIDENT_READ_PERMISSION, project_id=row.project_id):
        return None
    return EntityRef(entity_type="incident", entity_id=row.id, label=row.title)


async def _resolve_postmortem(
    session: AsyncSession, actor: Identity, entity_id: uuid.UUID
) -> EntityRef | None:
    row = await incidents_repository.get_postmortem_by_id(session, entity_id)
    if row is None or row.organization_id != actor.organization_id:
        return None
    if row.status not in ("approved", "published"):
        incident = await incidents_repository.get_incident_by_id(session, row.incident_id)
        project_id = incident.project_id if incident is not None else None
        if not (
            actor.has_permission(_POSTMORTEM_WRITE_PERMISSION, project_id=project_id)
            or actor.has_permission(_POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)
        ):
            return None
    return EntityRef(entity_type="postmortem", entity_id=row.id, label=None)


async def _resolve_document(
    session: AsyncSession, actor: Identity, entity_id: uuid.UUID
) -> EntityRef | None:
    row = await knowledge_repository.get_document_by_id(session, entity_id)
    if row is None or row.organization_id != actor.organization_id or row.deleted_at is not None:
        return None
    if row.status != "published" and not actor.has_permission(
        _REVIEW_PERMISSION, project_id=row.project_id
    ):
        return None
    return EntityRef(entity_type="document", entity_id=row.id, label=row.title)


async def _resolve_project(
    session: AsyncSession, actor: Identity, entity_id: uuid.UUID
) -> EntityRef | None:
    row = await tenancy_repository.get_project_by_id(session, entity_id)
    if row is None or row.organization_id != actor.organization_id:
        return None
    return EntityRef(entity_type="project", entity_id=row.id, label=row.name)


async def _resolve_investigation(
    session: AsyncSession, actor: Identity, entity_id: uuid.UUID
) -> EntityRef | None:
    row = await session.get(IncidentTimeline, entity_id)
    if (
        row is None
        or row.organization_id != actor.organization_id
        or row.event_type != "investigation"
    ):
        return None
    # An investigation's visibility rides entirely on its parent incident's
    # `incident:read` gate -- the same rule `core.incidents.service.
    # get_timeline` applies to every timeline entry.
    incident_ref = await _resolve_incident(session, actor, row.incident_id)
    if incident_ref is None:
        return None
    return EntityRef(
        entity_type="investigation",
        entity_id=row.id,
        label=f"Investigation on {row.occurred_at:%Y-%m-%d}",
    )


_RESOLVERS = {
    "incident": _resolve_incident,
    "postmortem": _resolve_postmortem,
    "document": _resolve_document,
    "project": _resolve_project,
    "investigation": _resolve_investigation,
}


async def _resolve_entity(
    session: AsyncSession, actor: Identity, entity_type: str, entity_id: uuid.UUID
) -> EntityRef | None:
    resolver = _RESOLVERS.get(entity_type)
    if resolver is None:
        return None
    return await resolver(session, actor, entity_id)


# --------------------------------------------------------------------------
# direct-relationship expansion -- foreign-key (live) + derived (stored)
# --------------------------------------------------------------------------


async def _fk_relationships(
    session: AsyncSession, actor: Identity, node: EntityRef
) -> list[GraphRelationship]:
    """Relationships resolved live from real foreign keys -- never read from
    `knowledge_graph_edges` because none are ever stored there.

    Deliberately asymmetric: `belongs_to` (incident/document -> project) is
    only ever expanded forward, not in reverse (project -> its incidents,
    project -> its documents). A project's memberships are not otherwise
    result-capped in this schema, so an unbounded reverse fan-out from a
    single project node is a real risk this module chooses not to take on
    without a paging design -- a genuine, documented scope limit rather than
    an oversight; see `docs/KNOWLEDGE_GRAPH.md`.
    """
    rels: list[GraphRelationship] = []

    if node.entity_type == "incident":
        incident = await incidents_repository.get_incident_by_id(session, node.entity_id)
        if incident is None:
            return rels

        project_ref = await _resolve_project(session, actor, incident.project_id)
        if project_ref is not None:
            spec = contract.get_spec("incident", "belongs_to", "project")
            rels.append(
                GraphRelationship(
                    source=node,
                    relationship_type="belongs_to",
                    target=project_ref,
                    provenance_type=spec.provenance_type,
                    meaning=spec.meaning,
                )
            )

        postmortem_row = await incidents_repository.get_postmortem_by_incident_id(
            session, node.entity_id
        )
        if postmortem_row is not None:
            postmortem_ref = await _resolve_postmortem(session, actor, postmortem_row.id)
            if postmortem_ref is not None:
                spec = contract.get_spec("incident", "has_postmortem", "postmortem")
                rels.append(
                    GraphRelationship(
                        source=node,
                        relationship_type="has_postmortem",
                        target=postmortem_ref,
                        provenance_type=spec.provenance_type,
                        meaning=spec.meaning,
                    )
                )

        timeline_rows = await incidents_repository.list_timeline_entries(session, node.entity_id)
        investigation_rows = [r for r in timeline_rows if r.event_type == "investigation"][
            :_MAX_INVESTIGATION_FANOUT
        ]
        if investigation_rows:
            spec = contract.get_spec("incident", "investigated_by", "investigation")
            for row in investigation_rows:
                investigation_ref = await _resolve_investigation(session, actor, row.id)
                if investigation_ref is not None:
                    rels.append(
                        GraphRelationship(
                            source=node,
                            relationship_type="investigated_by",
                            target=investigation_ref,
                            provenance_type=spec.provenance_type,
                            meaning=spec.meaning,
                        )
                    )

    elif node.entity_type == "postmortem":
        postmortem_row = await incidents_repository.get_postmortem_by_id(session, node.entity_id)
        if postmortem_row is not None:
            incident_ref = await _resolve_incident(session, actor, postmortem_row.incident_id)
            if incident_ref is not None:
                spec = contract.get_spec("incident", "has_postmortem", "postmortem")
                rels.append(
                    GraphRelationship(
                        source=incident_ref,
                        relationship_type="has_postmortem",
                        target=node,
                        provenance_type=spec.provenance_type,
                        meaning=spec.meaning,
                    )
                )

    elif node.entity_type == "document":
        document_row = await knowledge_repository.get_document_by_id(session, node.entity_id)
        if document_row is not None:
            project_ref = await _resolve_project(session, actor, document_row.project_id)
            if project_ref is not None:
                spec = contract.get_spec("document", "belongs_to", "project")
                rels.append(
                    GraphRelationship(
                        source=node,
                        relationship_type="belongs_to",
                        target=project_ref,
                        provenance_type=spec.provenance_type,
                        meaning=spec.meaning,
                    )
                )

    elif node.entity_type == "investigation":
        row = await session.get(IncidentTimeline, node.entity_id)
        if row is not None:
            incident_ref = await _resolve_incident(session, actor, row.incident_id)
            if incident_ref is not None:
                spec = contract.get_spec("incident", "investigated_by", "investigation")
                rels.append(
                    GraphRelationship(
                        source=incident_ref,
                        relationship_type="investigated_by",
                        target=node,
                        provenance_type=spec.provenance_type,
                        meaning=spec.meaning,
                    )
                )

    return rels


async def _derived_relationships(
    session: AsyncSession, actor: Identity, node: EntityRef
) -> list[GraphRelationship]:
    """Relationships read from stored `knowledge_graph_edges` rows.

    Resolves whichever endpoint is NOT `node` through `_resolve_entity` --
    an edge naming an entity `node`'s caller cannot see (deleted, or simply
    unauthorized) is silently dropped here, which is the query-time half of
    the lifecycle guarantee (see module docstring).
    """
    edges = await repository.get_direct_edges(
        session,
        organization_id=actor.organization_id,
        entity_type=node.entity_type,
        entity_id=node.entity_id,
    )
    rels: list[GraphRelationship] = []
    for edge in edges:
        node_is_source = (
            edge.source_entity_type == node.entity_type and edge.source_entity_id == node.entity_id
        )
        if node_is_source:
            other_ref = await _resolve_entity(
                session, actor, edge.target_entity_type, edge.target_entity_id
            )
            if other_ref is None:
                continue
            source, target = node, other_ref
        else:
            other_ref = await _resolve_entity(
                session, actor, edge.source_entity_type, edge.source_entity_id
            )
            if other_ref is None:
                continue
            source, target = other_ref, node

        try:
            meaning = contract.get_spec(
                source.entity_type, edge.relationship_type, target.entity_type
            ).meaning
        except contract.InvalidRelationshipError:  # pragma: no cover - defensive only
            meaning = edge.relationship_type

        rels.append(
            GraphRelationship(
                source=source,
                relationship_type=edge.relationship_type,
                target=target,
                provenance_type=edge.provenance_type,
                meaning=meaning,
                edge_id=edge.id,
            )
        )
    return rels


def _sort_key(rel: GraphRelationship) -> tuple:
    """Deterministic, explainable ordering -- foreign-key/deterministic
    relationships before manually-asserted ones, then a stable tiebreak on
    ids. No ranking model, no score: every field here comes straight off the
    relationship itself.
    """
    return (
        _PROVENANCE_ORDER.get(rel.provenance_type, 9),
        rel.relationship_type,
        str(rel.target.entity_id),
        str(rel.source.entity_id),
    )


async def _direct_relationships(
    session: AsyncSession, actor: Identity, node: EntityRef
) -> list[GraphRelationship]:
    combined = await _fk_relationships(session, actor, node)
    combined += await _derived_relationships(session, actor, node)
    combined.sort(key=_sort_key)
    return combined


# --------------------------------------------------------------------------
# public traversal API
# --------------------------------------------------------------------------


async def get_neighborhood(
    session: AsyncSession,
    actor: Identity,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    max_depth: int = MAX_TRAVERSAL_DEPTH,
) -> GraphNeighborhood:
    """Bounded, authorized breadth-first traversal from one entity.

    `max_depth` can only ever NARROW `MAX_TRAVERSAL_DEPTH` -- a caller
    cannot pass a larger value and get more hops than the hard ceiling
    allows; there is no parameter that widens it. Node and edge counts are
    capped by `DEFAULT_MAX_NODES`/`DEFAULT_MAX_EDGES`, enforced during
    expansion (a walk that has already hit a cap stops adding to the result
    rather than continuing to do work that gets discarded), and `truncated`
    is set whenever either cap was actually hit.

    Cycle protection is structural: a node is only ever added to the next
    frontier the first time it is reached (`visited`), so a relationship
    that loops back to an already-visited node contributes its edge (if
    that edge itself is new) but never re-expands from that node.
    """
    if entity_type not in contract.ENTITY_TYPES:
        raise ValidationError(
            "Unknown entity type.",
            error_code="graph.invalid_entity_type",
            detail={"entity_type": entity_type},
        )

    depth_cap = max(1, min(max_depth, MAX_TRAVERSAL_DEPTH))

    origin = await _resolve_entity(session, actor, entity_type, entity_id)
    if origin is None:
        raise NotFoundError(
            "Entity not found.",
            error_code="graph.entity_not_found",
            detail={"entity_type": entity_type, "entity_id": str(entity_id)},
        )

    visited: dict[tuple[str, uuid.UUID], EntityRef] = {origin.key: origin}
    seen_edges: set[tuple] = set()
    relationships: list[GraphRelationship] = []
    truncated = False
    frontier = [origin]
    depth_reached = 0

    for depth in range(1, depth_cap + 1):
        if not frontier:
            break
        next_frontier: list[EntityRef] = []
        for node in frontier:
            direct = await _direct_relationships(session, actor, node)
            for rel in direct:
                edge_key = (rel.source.key, rel.relationship_type, rel.target.key)
                if edge_key in seen_edges:
                    continue
                if len(relationships) >= DEFAULT_MAX_EDGES:
                    truncated = True
                    break
                seen_edges.add(edge_key)
                relationships.append(rel.model_copy(update={"depth": depth}))

                other = rel.target if rel.target.key != node.key else rel.source
                if other.key not in visited:
                    if len(visited) >= DEFAULT_MAX_NODES:
                        truncated = True
                        continue
                    visited[other.key] = other
                    next_frontier.append(other)
            if len(relationships) >= DEFAULT_MAX_EDGES:
                truncated = True
                break
        depth_reached = depth
        frontier = next_frontier

    return GraphNeighborhood(
        origin=origin,
        relationships=relationships,
        nodes=list(visited.values()),
        max_depth_reached=depth_reached,
        truncated=truncated,
    )


async def get_direct_relationships(
    session: AsyncSession, actor: Identity, entity_type: str, entity_id: uuid.UUID
) -> list[GraphRelationship]:
    """Depth-1 relationships only -- a thin, explicit alias over
    `get_neighborhood` for the "what's directly connected" API surface.
    """
    neighborhood = await get_neighborhood(session, actor, entity_type, entity_id, max_depth=1)
    return neighborhood.relationships


# --------------------------------------------------------------------------
# manual relationship creation -- the one assertable derived relationship
# --------------------------------------------------------------------------


async def create_manual_relationship(
    session: AsyncSession, actor: Identity, data: RelationshipCreate
) -> GraphRelationship:
    """Assert `incident related_to incident` -- currently the only manual
    relationship the contract defines (`contract.DERIVED_RELATIONSHIPS`).

    Requires `incident:write` on BOTH incidents' own projects, not just one
    -- asserting a relationship touches both entities, so both must be
    writable by this actor, mirroring the "authorization applies to both
    ends" rule this whole module is built around.

    Fails loudly (`ValidationError`) rather than silently succeeding if the
    contract ever grows a second manual relationship type between entities
    this function does not yet know how to authorize -- an unimplemented
    authorization rule must never be mistaken for an absent one.
    """
    try:
        spec = contract.get_derived_spec(
            data.source_entity_type, data.relationship_type, data.target_entity_type
        )
    except contract.InvalidRelationshipError as exc:
        # Translated at the boundary, per `InvalidRelationshipError`'s own
        # docstring -- everything below this point deals in the project's
        # own exception vocabulary, not a `core.graph`-internal one.
        raise ValidationError(
            str(exc),
            error_code="graph.invalid_relationship",
            detail={
                "source_entity_type": data.source_entity_type,
                "relationship_type": data.relationship_type,
                "target_entity_type": data.target_entity_type,
            },
        ) from exc
    if spec.provenance_type != "manual":
        raise ValidationError(
            "This relationship is not manually assertable.",
            error_code="graph.not_manual",
            detail={"relationship_type": data.relationship_type},
        )

    source_ref = await _resolve_entity(
        session, actor, data.source_entity_type, data.source_entity_id
    )
    if source_ref is None:
        raise NotFoundError(
            "Source entity not found.",
            error_code="graph.entity_not_found",
            detail={
                "entity_type": data.source_entity_type,
                "entity_id": str(data.source_entity_id),
            },
        )
    target_ref = await _resolve_entity(
        session, actor, data.target_entity_type, data.target_entity_id
    )
    if target_ref is None:
        raise NotFoundError(
            "Target entity not found.",
            error_code="graph.entity_not_found",
            detail={
                "entity_type": data.target_entity_type,
                "entity_id": str(data.target_entity_id),
            },
        )

    if (spec.source_type, spec.relationship_type, spec.target_type) != (
        "incident",
        "related_to",
        "incident",
    ):
        raise ValidationError(  # pragma: no cover - unreachable until the contract grows
            "No authorization rule implemented for this manual relationship type.",
            error_code="graph.manual_not_supported",
            detail={"relationship_type": data.relationship_type},
        )

    source_incident = await incidents_repository.get_incident_by_id(
        session, data.source_entity_id
    )
    target_incident = await incidents_repository.get_incident_by_id(
        session, data.target_entity_id
    )
    require_project_permission(actor, source_incident.project_id, _INCIDENT_WRITE_PERMISSION)
    require_project_permission(actor, target_incident.project_id, _INCIDENT_WRITE_PERMISSION)

    source_id, target_id = contract.canonical_direction(
        spec, data.source_entity_id, data.target_entity_id
    )
    if source_id == data.source_entity_id:
        canonical_source_ref, canonical_target_ref = source_ref, target_ref
    else:
        canonical_source_ref, canonical_target_ref = target_ref, source_ref

    # `project_id` is a narrowing hint only (see `graph_models.py`), and a
    # symmetric incident<->incident relationship has no single owning
    # project -- left unset rather than arbitrarily picking one side.
    row, action = await repository.upsert_derived_edge(
        session,
        organization_id=actor.organization_id,
        project_id=None,
        source_entity_type=spec.source_type,
        source_entity_id=source_id,
        relationship_type=spec.relationship_type,
        target_entity_type=spec.target_type,
        target_entity_id=target_id,
        provenance_type="manual",
        provenance_id=None,
        created_by=actor.audit_tag,
    )

    await record_audit_event(
        session,
        actor,
        action=f"graph.relationship.{action}",
        resource_type="knowledge_graph_edge",
        resource_id=row.id,
        metadata={
            "relationship_type": spec.relationship_type,
            "source_entity_type": spec.source_type,
            "source_entity_id": str(source_id),
            "target_entity_type": spec.target_type,
            "target_entity_id": str(target_id),
        },
    )
    logger.info(
        "graph_manual_relationship_asserted",
        edge_id=str(row.id),
        action=action,
        actor=actor.audit_tag,
    )

    return GraphRelationship(
        source=canonical_source_ref,
        relationship_type=spec.relationship_type,
        target=canonical_target_ref,
        provenance_type="manual",
        meaning=spec.meaning,
        edge_id=row.id,
        depth=1,
    )


# --------------------------------------------------------------------------
# deterministic discovery -- no LLM, a straight field read
# --------------------------------------------------------------------------


async def discover_document_incident_edges(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    created_by: str = "system:graph_discovery",
) -> DiscoveryResult:
    """Discover (and repair) every `document --documents--> incident` edge
    for one organization.

    READ HALF: scans `document_metadata` rows keyed `source_incident_id`
    (written by `core.knowledge.service.propose_document`) and upserts the
    corresponding edge for each -- a direct field read, not an inference; no
    model is involved anywhere in this pass.

    REPAIR HALF: every currently-active edge of this relationship type whose
    document or incident no longer resolves (wrong/no organization, or the
    document is soft-deleted) is deactivated. This is what makes the graph
    reconstructable by rerunning discovery, and is a second, independent
    lifecycle barrier alongside `remove_edges_for_entity`'s direct hook.

    Runs unscoped by `Identity` -- like `core.tenancy.service.
    list_organizations`, this is a system-level maintenance pass over one
    organization's own data, not a request made on a particular user's
    behalf (there is no LLM call and nothing about the result depends on who
    triggers it). Callers are internal/operational, e.g. a manual or
    scheduled invocation per organization -- see `docs/KNOWLEDGE_GRAPH.md`
    for why this is not (yet) wired into an always-running scheduler.
    """
    spec = contract.get_derived_spec("document", "documents", "incident")

    stmt = select(DocumentMetadata).where(
        DocumentMetadata.key == _SOURCE_INCIDENT_METADATA_KEY
    )
    metadata_rows = (await session.execute(stmt)).scalars().all()

    created = revived = unchanged = 0
    for meta_row in metadata_rows:
        document_row = await knowledge_repository.get_document_by_id(
            session, meta_row.document_id
        )
        if (
            document_row is None
            or document_row.organization_id != organization_id
            or document_row.deleted_at is not None
        ):
            continue
        try:
            incident_id = uuid.UUID(meta_row.value)
        except (ValueError, TypeError, AttributeError):
            continue
        incident_row = await incidents_repository.get_incident_by_id(session, incident_id)
        if incident_row is None or incident_row.organization_id != organization_id:
            continue

        _, action = await repository.upsert_derived_edge(
            session,
            organization_id=organization_id,
            project_id=document_row.project_id,
            source_entity_type="document",
            source_entity_id=document_row.id,
            relationship_type=spec.relationship_type,
            target_entity_type="incident",
            target_entity_id=incident_row.id,
            provenance_type=spec.provenance_type,
            provenance_id=meta_row.id,
            created_by=created_by,
        )
        if action == "created":
            created += 1
        elif action == "revived":
            revived += 1
        else:
            unchanged += 1

    removed_stale = 0
    existing_edges = await repository.list_active_edges_by_relationship_type(
        session, organization_id=organization_id, relationship_type=spec.relationship_type
    )
    for edge in existing_edges:
        document_row = await knowledge_repository.get_document_by_id(
            session, edge.source_entity_id
        )
        incident_row = await incidents_repository.get_incident_by_id(
            session, edge.target_entity_id
        )
        document_gone = (
            document_row is None
            or document_row.organization_id != organization_id
            or document_row.deleted_at is not None
        )
        incident_gone = incident_row is None or incident_row.organization_id != organization_id
        if document_gone or incident_gone:
            removed_stale += await repository.deactivate_edge(session, edge.id)

    result = DiscoveryResult(
        edges_created=created,
        edges_revived=revived,
        edges_unchanged=unchanged,
        edges_removed_stale=removed_stale,
        scanned_at=datetime.now(UTC),
    )
    logger.info(
        "graph_discovery_completed",
        organization_id=str(organization_id),
        edges_created=created,
        edges_revived=revived,
        edges_unchanged=unchanged,
        edges_removed_stale=removed_stale,
    )
    return result


# --------------------------------------------------------------------------
# lifecycle integration -- the physical-cleanup hook other modules call
# --------------------------------------------------------------------------


async def remove_edges_for_entity(
    session: AsyncSession, *, organization_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> int:
    """Physically deactivate every edge naming `(entity_type, entity_id)` as
    either endpoint. Returns the number of edges deactivated.

    The lifecycle-cleanup hook another module calls once IT has confirmed an
    entity is gone -- today, `core.knowledge.service.reject_document` is the
    only real caller, because document soft-delete is the only deletion path
    that exists for any entity type this graph covers (`incidents.deleted_at`
    /`postmortems.deleted_at` are declared but dead code -- see
    `docs/KNOWLEDGE_GRAPH.md`). Takes `organization_id` directly rather than
    an `Identity`: this runs as the second half of a deletion the caller
    already authorized, not a new access decision of its own.
    """
    return await repository.deactivate_edges_touching_entity(
        session, organization_id=organization_id, entity_type=entity_type, entity_id=entity_id
    )
