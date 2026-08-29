"""Tests for `app.core.graph.service` -- authorized entity resolution,
bounded traversal, manual-relationship creation, deterministic discovery,
and lifecycle cleanup.

Every repository this service reads from (its own, plus `core.incidents`/
`core.knowledge`/`core.tenancy`) is monkeypatched with a small in-memory
fake -- the same style `tests/core/memory/test_service.py` uses for
`memory_service.repository`. No database, no LLM: this whole module has
neither.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.graph import service as graph_service
from app.core.graph.schemas import RelationshipCreate
from app.database.models.core_models import IncidentTimeline
from app.shared.schemas import ActorKind, Identity

# --------------------------------------------------------------------------
# fake rows
# --------------------------------------------------------------------------


class _Incident:
    def __init__(self, *, id, organization_id, project_id, title="An incident"):
        self.id = id
        self.organization_id = organization_id
        self.project_id = project_id
        self.title = title


class _Postmortem:
    def __init__(self, *, id, organization_id, incident_id, status="draft"):
        self.id = id
        self.organization_id = organization_id
        self.incident_id = incident_id
        self.status = status


class _Document:
    def __init__(
        self,
        *,
        id,
        organization_id,
        project_id,
        title="A document",
        status="published",
        deleted_at=None,
    ):
        self.id = id
        self.organization_id = organization_id
        self.project_id = project_id
        self.title = title
        self.status = status
        self.deleted_at = deleted_at


class _Project:
    def __init__(self, *, id, organization_id, name="A project"):
        self.id = id
        self.organization_id = organization_id
        self.name = name


class _Timeline:
    def __init__(self, *, id, organization_id, incident_id, event_type="investigation"):
        self.id = id
        self.organization_id = organization_id
        self.incident_id = incident_id
        self.event_type = event_type
        self.occurred_at = datetime(2026, 1, 1, tzinfo=UTC)


class _Edge:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.organization_id = kwargs["organization_id"]
        self.project_id = kwargs.get("project_id")
        self.source_entity_type = kwargs["source_entity_type"]
        self.source_entity_id = kwargs["source_entity_id"]
        self.relationship_type = kwargs["relationship_type"]
        self.target_entity_type = kwargs["target_entity_type"]
        self.target_entity_id = kwargs["target_entity_id"]
        self.provenance_type = kwargs["provenance_type"]
        self.provenance_id = kwargs.get("provenance_id")
        self.status = kwargs.get("status", "active")
        self.created_by = kwargs["created_by"]
        self.edge_metadata = kwargs.get("edge_metadata")


class _DocMeta:
    def __init__(self, *, id, document_id, key, value):
        self.id = id
        self.document_id = document_id
        self.key = key
        self.value = value


class _FakeSession:
    """Only what `service.py` actually calls directly: `session.get` for
    `IncidentTimeline`, used to resolve/expand investigation nodes."""

    def __init__(self, state: dict):
        self._state = state

    async def get(self, model, entity_id):
        assert model is IncidentTimeline
        return self._state["timeline"].get(entity_id)


def _user(organization_id: uuid.UUID, *, projects: dict | None = None) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions=projects or {},
    )


@pytest.fixture()
def world(monkeypatch):
    state = {
        "incidents": {},
        "postmortems": {},
        "documents": {},
        "projects": {},
        "timeline": {},
        "doc_metadata": [],
        "edges": {},
    }

    async def fake_get_incident_by_id(session, incident_id):
        return state["incidents"].get(incident_id)

    async def fake_get_postmortem_by_id(session, postmortem_id):
        return state["postmortems"].get(postmortem_id)

    async def fake_get_postmortem_by_incident_id(session, incident_id):
        for p in state["postmortems"].values():
            if p.incident_id == incident_id:
                return p
        return None

    async def fake_list_timeline_entries(session, incident_id):
        return [t for t in state["timeline"].values() if t.incident_id == incident_id]

    monkeypatch.setattr(
        graph_service.incidents_repository, "get_incident_by_id", fake_get_incident_by_id
    )
    monkeypatch.setattr(
        graph_service.incidents_repository, "get_postmortem_by_id", fake_get_postmortem_by_id
    )
    monkeypatch.setattr(
        graph_service.incidents_repository,
        "get_postmortem_by_incident_id",
        fake_get_postmortem_by_incident_id,
    )
    monkeypatch.setattr(
        graph_service.incidents_repository, "list_timeline_entries", fake_list_timeline_entries
    )

    async def fake_get_document_by_id(session, document_id):
        return state["documents"].get(document_id)

    monkeypatch.setattr(
        graph_service.knowledge_repository, "get_document_by_id", fake_get_document_by_id
    )

    async def fake_get_project_by_id(session, project_id):
        return state["projects"].get(project_id)

    monkeypatch.setattr(
        graph_service.tenancy_repository, "get_project_by_id", fake_get_project_by_id
    )

    async def fake_get_direct_edges(session, *, organization_id, entity_type, entity_id):
        return [
            e
            for e in state["edges"].values()
            if e.organization_id == organization_id
            and e.status == "active"
            and (
                (e.source_entity_type, e.source_entity_id) == (entity_type, entity_id)
                or (e.target_entity_type, e.target_entity_id) == (entity_type, entity_id)
            )
        ]

    async def fake_upsert_derived_edge(
        session,
        *,
        organization_id,
        project_id,
        source_entity_type,
        source_entity_id,
        relationship_type,
        target_entity_type,
        target_entity_id,
        provenance_type,
        provenance_id,
        created_by,
        edge_metadata=None,
    ):
        key = (
            organization_id,
            source_entity_type,
            source_entity_id,
            relationship_type,
            target_entity_type,
            target_entity_id,
        )
        existing = state["edges"].get(key)
        if existing is None:
            edge = _Edge(
                organization_id=organization_id,
                project_id=project_id,
                source_entity_type=source_entity_type,
                source_entity_id=source_entity_id,
                relationship_type=relationship_type,
                target_entity_type=target_entity_type,
                target_entity_id=target_entity_id,
                provenance_type=provenance_type,
                provenance_id=provenance_id,
                created_by=created_by,
                edge_metadata=edge_metadata,
            )
            state["edges"][key] = edge
            return edge, "created"
        if existing.status != "active":
            existing.status = "active"
            existing.created_by = created_by
            return existing, "revived"
        return existing, "unchanged"

    async def fake_list_active_edges_by_relationship_type(
        session, *, organization_id, relationship_type
    ):
        return [
            e
            for e in state["edges"].values()
            if e.organization_id == organization_id
            and e.status == "active"
            and e.relationship_type == relationship_type
        ]

    async def fake_deactivate_edge(session, edge_id):
        for e in state["edges"].values():
            if e.id == edge_id and e.status == "active":
                e.status = "removed"
                return 1
        return 0

    async def fake_deactivate_edges_touching_entity(
        session, *, organization_id, entity_type, entity_id
    ):
        count = 0
        for e in state["edges"].values():
            if (
                e.organization_id == organization_id
                and e.status == "active"
                and (
                    (e.source_entity_type, e.source_entity_id) == (entity_type, entity_id)
                    or (e.target_entity_type, e.target_entity_id) == (entity_type, entity_id)
                )
            ):
                e.status = "removed"
                count += 1
        return count

    monkeypatch.setattr(graph_service.repository, "get_direct_edges", fake_get_direct_edges)
    monkeypatch.setattr(graph_service.repository, "upsert_derived_edge", fake_upsert_derived_edge)
    monkeypatch.setattr(
        graph_service.repository,
        "list_active_edges_by_relationship_type",
        fake_list_active_edges_by_relationship_type,
    )
    monkeypatch.setattr(graph_service.repository, "deactivate_edge", fake_deactivate_edge)
    monkeypatch.setattr(
        graph_service.repository,
        "deactivate_edges_touching_entity",
        fake_deactivate_edges_touching_entity,
    )

    async def fake_audit(session, actor, **kwargs):
        state.setdefault("audit", []).append(kwargs)

    monkeypatch.setattr(graph_service, "record_audit_event", fake_audit)

    return state


@pytest.fixture()
def session(world):
    return _FakeSession(world)


# --------------------------------------------------------------------------
# entity resolution / authorization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_organization_incident_is_not_found(world, session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    incident_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=org_a, project_id=uuid.uuid4()
    )
    actor = _user(org_b, projects={})

    with pytest.raises(NotFoundError):
        await graph_service.get_neighborhood(session, actor, "incident", incident_id)


@pytest.mark.asyncio
async def test_project_scoped_permission_gates_incident_visibility(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )

    no_access = _user(organization_id, projects={project_id: frozenset()})
    with pytest.raises(NotFoundError):
        await graph_service.get_neighborhood(session, no_access, "incident", incident_id)

    has_access = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    neighborhood = await graph_service.get_neighborhood(
        session, has_access, "incident", incident_id
    )
    assert neighborhood.origin.entity_id == incident_id


@pytest.mark.asyncio
async def test_deleted_document_target_is_dropped_from_traversal(world, session):
    """The second final-report invariant: a deleted source/target must not
    leak through a derived edge."""
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id,
        organization_id=organization_id,
        project_id=project_id,
        status="published",
        deleted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    edge = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )
    world["edges"][("k",)] = edge

    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    neighborhood = await graph_service.get_neighborhood(session, actor, "incident", incident_id)

    assert neighborhood.relationships == [], "an edge to a deleted document must not surface"
    assert {n.entity_id for n in neighborhood.nodes} == {incident_id}


@pytest.mark.asyncio
async def test_unpublished_document_requires_review_permission(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="proposed"
    )
    world["edges"][("k",)] = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )

    reader_only = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    neighborhood = await graph_service.get_neighborhood(
        session, reader_only, "incident", incident_id
    )
    assert neighborhood.relationships == [], "an unpublished document requires knowledge:review"

    reviewer = _user(
        organization_id,
        projects={project_id: frozenset({"incident:read", "knowledge:review"})},
    )
    neighborhood = await graph_service.get_neighborhood(session, reviewer, "incident", incident_id)
    assert len(neighborhood.relationships) == 1
    assert neighborhood.relationships[0].source.entity_id == document_id


# --------------------------------------------------------------------------
# direct relationships: foreign-key + derived, provenance preserved
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_direct_relationships_include_project_and_postmortem_and_investigation(
    world, session
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    postmortem_id = uuid.uuid4()
    timeline_id = uuid.uuid4()

    world["projects"][project_id] = _Project(id=project_id, organization_id=organization_id)
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["postmortems"][postmortem_id] = _Postmortem(
        id=postmortem_id,
        organization_id=organization_id,
        incident_id=incident_id,
        status="approved",
    )
    world["timeline"][timeline_id] = _Timeline(
        id=timeline_id, organization_id=organization_id, incident_id=incident_id
    )

    actor = _user(
        organization_id,
        projects={project_id: frozenset({"incident:read", "postmortem:write"})},
    )
    rels = await graph_service.get_direct_relationships(session, actor, "incident", incident_id)

    by_type = {r.relationship_type: r for r in rels}
    assert set(by_type) == {"belongs_to", "has_postmortem", "investigated_by"}
    assert by_type["belongs_to"].provenance_type == "foreign_key"
    assert by_type["belongs_to"].target.entity_id == project_id
    assert by_type["has_postmortem"].target.entity_id == postmortem_id
    assert by_type["investigated_by"].target.entity_id == timeline_id


@pytest.mark.asyncio
async def test_unreviewed_postmortem_is_not_reachable_without_permission(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    postmortem_id = uuid.uuid4()
    world["projects"][project_id] = _Project(id=project_id, organization_id=organization_id)
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["postmortems"][postmortem_id] = _Postmortem(
        id=postmortem_id, organization_id=organization_id, incident_id=incident_id, status="draft"
    )

    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    rels = await graph_service.get_direct_relationships(session, actor, "incident", incident_id)
    assert "has_postmortem" not in {r.relationship_type for r in rels}


@pytest.mark.asyncio
async def test_derived_document_incident_edge_is_traversable_from_either_side(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    world["edges"][("k",)] = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )
    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    from_incident = await graph_service.get_direct_relationships(
        session, actor, "incident", incident_id
    )
    from_document = await graph_service.get_direct_relationships(
        session, actor, "document", document_id
    )

    assert len(from_incident) == 1 and from_incident[0].relationship_type == "documents"
    assert len(from_document) == 1 and from_document[0].relationship_type == "documents"
    assert from_incident[0].source.entity_id == document_id
    assert from_incident[0].target.entity_id == incident_id


# --------------------------------------------------------------------------
# bounded, cycle-safe traversal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_hop_traversal_reaches_depth_two_but_not_further(world, session):
    """document --documents--> incident --has_postmortem--> postmortem is a
    genuine two-hop chain from the document."""
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    postmortem_id = uuid.uuid4()

    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    world["postmortems"][postmortem_id] = _Postmortem(
        id=postmortem_id,
        organization_id=organization_id,
        incident_id=incident_id,
        status="approved",
    )
    world["edges"][("k",)] = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )

    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    neighborhood = await graph_service.get_neighborhood(session, actor, "document", document_id)

    node_ids = {n.entity_id for n in neighborhood.nodes}
    assert node_ids == {document_id, incident_id, postmortem_id}
    assert neighborhood.max_depth_reached == 2
    depths = {r.relationship_type: r.depth for r in neighborhood.relationships}
    assert depths["documents"] == 1
    assert depths["has_postmortem"] == 2


@pytest.mark.asyncio
async def test_depth_one_stops_after_the_first_hop(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    postmortem_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    world["postmortems"][postmortem_id] = _Postmortem(
        id=postmortem_id,
        organization_id=organization_id,
        incident_id=incident_id,
        status="approved",
    )
    world["edges"][("k",)] = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )
    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    neighborhood = await graph_service.get_neighborhood(
        session, actor, "document", document_id, max_depth=1
    )
    assert {n.entity_id for n in neighborhood.nodes} == {document_id, incident_id}


@pytest.mark.asyncio
async def test_caller_supplied_depth_cannot_exceed_the_hard_ceiling(world, session, monkeypatch):
    """`max_depth` may only narrow `MAX_TRAVERSAL_DEPTH`, never widen it."""
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    neighborhood = await graph_service.get_neighborhood(
        session, actor, "incident", incident_id, max_depth=999
    )
    assert neighborhood.max_depth_reached <= graph_service.MAX_TRAVERSAL_DEPTH


@pytest.mark.asyncio
async def test_symmetric_related_to_cycle_does_not_loop_forever(world, session):
    """incident A <-related_to-> incident B, traversed from A, must
    terminate and must not expand B back into A a second time."""
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_a, incident_b = uuid.uuid4(), uuid.uuid4()
    world["incidents"][incident_a] = _Incident(
        id=incident_a, organization_id=organization_id, project_id=project_id, title="A"
    )
    world["incidents"][incident_b] = _Incident(
        id=incident_b, organization_id=organization_id, project_id=project_id, title="B"
    )
    world["edges"][("k",)] = _Edge(
        organization_id=organization_id,
        source_entity_type="incident",
        source_entity_id=incident_a,
        relationship_type="related_to",
        target_entity_type="incident",
        target_entity_id=incident_b,
        provenance_type="manual",
        created_by="user:someone",
    )
    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    neighborhood = await graph_service.get_neighborhood(session, actor, "incident", incident_a)

    assert {n.entity_id for n in neighborhood.nodes} == {incident_a, incident_b}
    related_to_edges = [
        r for r in neighborhood.relationships if r.relationship_type == "related_to"
    ]
    assert len(related_to_edges) == 1, "the same edge must not be counted twice via both endpoints"


@pytest.mark.asyncio
async def test_traversal_truncates_and_reports_it_when_the_edge_cap_is_hit(
    world, session, monkeypatch
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["projects"][project_id] = _Project(id=project_id, organization_id=organization_id)
    actor = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    monkeypatch.setattr(graph_service, "DEFAULT_MAX_EDGES", 0)
    neighborhood = await graph_service.get_neighborhood(session, actor, "incident", incident_id)
    assert neighborhood.truncated is True
    assert neighborhood.relationships == []


# --------------------------------------------------------------------------
# manual relationship creation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_manual_relationship_requires_write_on_both_incidents(world, session):
    organization_id = uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    incident_a, incident_b = uuid.uuid4(), uuid.uuid4()
    world["incidents"][incident_a] = _Incident(
        id=incident_a, organization_id=organization_id, project_id=project_a
    )
    world["incidents"][incident_b] = _Incident(
        id=incident_b, organization_id=organization_id, project_id=project_b
    )

    # Write access on A's project only -- must still be denied.
    partial_actor = _user(
        organization_id,
        projects={
            project_a: frozenset({"incident:read", "incident:write"}),
            project_b: frozenset({"incident:read"}),
        },
    )
    with pytest.raises(PermissionDeniedError):
        await graph_service.create_manual_relationship(
            session,
            partial_actor,
            RelationshipCreate(
                source_entity_type="incident",
                source_entity_id=incident_a,
                relationship_type="related_to",
                target_entity_type="incident",
                target_entity_id=incident_b,
            ),
        )


@pytest.mark.asyncio
async def test_create_manual_relationship_succeeds_and_is_symmetric_regardless_of_argument_order(
    world, session
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_a, incident_b = uuid.uuid4(), uuid.uuid4()
    world["incidents"][incident_a] = _Incident(
        id=incident_a, organization_id=organization_id, project_id=project_id
    )
    world["incidents"][incident_b] = _Incident(
        id=incident_b, organization_id=organization_id, project_id=project_id
    )
    actor = _user(
        organization_id, projects={project_id: frozenset({"incident:read", "incident:write"})}
    )

    forward = await graph_service.create_manual_relationship(
        session,
        actor,
        RelationshipCreate(
            source_entity_type="incident",
            source_entity_id=incident_a,
            relationship_type="related_to",
            target_entity_type="incident",
            target_entity_id=incident_b,
        ),
    )
    backward = await graph_service.create_manual_relationship(
        session,
        actor,
        RelationshipCreate(
            source_entity_type="incident",
            source_entity_id=incident_b,
            relationship_type="related_to",
            target_entity_type="incident",
            target_entity_id=incident_a,
        ),
    )

    assert forward.edge_id == backward.edge_id, (
        "asserting it either direction must converge on one edge"
    )
    assert len(world["edges"]) == 1


@pytest.mark.asyncio
async def test_create_manual_relationship_rejects_a_foreign_key_backed_type(world, session):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    postmortem_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["postmortems"][postmortem_id] = _Postmortem(
        id=postmortem_id,
        organization_id=organization_id,
        incident_id=incident_id,
        status="approved",
    )
    actor = _user(
        organization_id, projects={project_id: frozenset({"incident:read", "incident:write"})}
    )
    with pytest.raises(ValidationError):
        await graph_service.create_manual_relationship(
            session,
            actor,
            RelationshipCreate(
                source_entity_type="incident",
                source_entity_id=incident_id,
                relationship_type="has_postmortem",
                target_entity_type="postmortem",
                target_entity_id=postmortem_id,
            ),
        )


# --------------------------------------------------------------------------
# lifecycle: remove_edges_for_entity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_edges_for_entity_deactivates_every_edge_touching_it(world, session):
    organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    edge = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )
    world["edges"][("k",)] = edge

    removed = await graph_service.remove_edges_for_entity(
        session, organization_id=organization_id, entity_type="document", entity_id=document_id
    )
    assert removed == 1
    assert edge.status == "removed"


# --------------------------------------------------------------------------
# deterministic discovery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_creates_an_edge_from_document_metadata(world, session, monkeypatch):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    meta_rows = [
        _DocMeta(
            id=uuid.uuid4(),
            document_id=document_id,
            key="source_incident_id",
            value=str(incident_id),
        )
    ]
    _patch_metadata_scan(monkeypatch, meta_rows)

    result = await graph_service.discover_document_incident_edges(
        session, organization_id=organization_id
    )
    assert result.edges_created == 1
    assert result.edges_removed_stale == 0


@pytest.mark.asyncio
async def test_discovery_skips_metadata_from_another_organization(world, session, monkeypatch):
    organization_id = uuid.uuid4()
    other_org = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_id = uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    # Document belongs to a DIFFERENT organization than the one being scanned.
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=other_org, project_id=project_id, status="published"
    )
    meta_rows = [
        _DocMeta(
            id=uuid.uuid4(),
            document_id=document_id,
            key="source_incident_id",
            value=str(incident_id),
        )
    ]
    _patch_metadata_scan(monkeypatch, meta_rows)

    result = await graph_service.discover_document_incident_edges(
        session, organization_id=organization_id
    )
    assert result.edges_created == 0


@pytest.mark.asyncio
async def test_discovery_repairs_a_stale_edge_when_the_incident_is_gone(
    world, session, monkeypatch
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    document_id = uuid.uuid4()
    incident_id = uuid.uuid4()  # deliberately never added to world["incidents"]
    world["documents"][document_id] = _Document(
        id=document_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    edge = _Edge(
        organization_id=organization_id,
        source_entity_type="document",
        source_entity_id=document_id,
        relationship_type="documents",
        target_entity_type="incident",
        target_entity_id=incident_id,
        provenance_type="deterministic_extraction",
        created_by="agent:graph_discovery",
    )
    world["edges"][("stale",)] = edge
    _patch_metadata_scan(monkeypatch, [])

    result = await graph_service.discover_document_incident_edges(
        session, organization_id=organization_id
    )
    assert result.edges_removed_stale == 1
    assert edge.status == "removed"


def _patch_metadata_scan(monkeypatch, rows):
    """`discover_document_incident_edges` runs a plain `select(DocumentMetadata)
    ...` -- patch `session.execute` (only used for that one query in this
    service) to hand back the fixed row set a test wants scanned."""

    class _MetaResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    async def fake_execute(self, statement, *args, **kwargs):
        return _MetaResult(rows)

    monkeypatch.setattr(_FakeSession, "execute", fake_execute, raising=False)
