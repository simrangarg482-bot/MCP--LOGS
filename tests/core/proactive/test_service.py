"""Tests for `app.core.proactive.service` -- detectors, detection
orchestration (upsert/reconcile/idempotency/failure-isolation), authorized
finding resolution (tenant + permission isolation, mixed visibility), and
the lifecycle-cleanup hook.

Every repository this service reads from (its own, plus `core.incidents`/
`core.knowledge`/`core.graph`) is monkeypatched with a small in-memory
fake -- the same style `tests/core/graph/test_service.py` uses. No
database, no LLM: this whole module has neither.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import NotFoundError
from app.core.proactive import contract
from app.core.proactive import service as proactive_service
from app.shared.schemas import ActorKind, Identity

# --------------------------------------------------------------------------
# fake rows
# --------------------------------------------------------------------------


class _Incident:
    def __init__(
        self,
        *,
        id,
        organization_id,
        project_id,
        severity="high",
        created_at=None,
        title="An incident",
    ):
        self.id = id
        self.organization_id = organization_id
        self.project_id = project_id
        self.severity = severity
        self.created_at = created_at or datetime.now(UTC)
        self.title = title


class _Document:
    def __init__(
        self,
        *,
        id,
        organization_id,
        project_id,
        status="published",
        deleted_at=None,
        title="A document",
    ):
        self.id = id
        self.organization_id = organization_id
        self.project_id = project_id
        self.status = status
        self.deleted_at = deleted_at
        self.title = title


class _Edge:
    def __init__(self, *, organization_id, source_entity_id, target_entity_id):
        self.organization_id = organization_id
        self.source_entity_id = source_entity_id
        self.target_entity_id = target_entity_id


class _FindingRow:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.organization_id = kwargs["organization_id"]
        self.project_id = kwargs.get("project_id")
        self.finding_type = kwargs["finding_type"]
        self.status = kwargs.get("status", "active")
        self.title = kwargs.get("title", "")
        self.summary = kwargs.get("summary", "")
        self.fingerprint = kwargs["fingerprint"]
        self.support_count = kwargs.get("support_count", 0)
        self.detector_name = kwargs.get("detector_name", "")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.first_seen_at = kwargs.get("first_seen_at", now)
        self.last_seen_at = kwargs.get("last_seen_at", now)
        self.deactivated_at = kwargs.get("deactivated_at")
        self.created_at = now
        self.updated_at = now


class _EvidenceRow:
    def __init__(self, *, finding_id, entity_type, entity_id, role):
        self.finding_id = finding_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.role = role


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
        "documents": {},
        "edges": [],
        "findings": {},
        "evidence": {},
        "audit": [],
    }

    async def fake_list_incidents_by_severity_since(session, organization_id, *, severities, since):
        return [
            row
            for row in state["incidents"].values()
            if row.organization_id == organization_id
            and row.severity in severities
            and row.created_at >= since
        ]

    async def fake_get_incident_by_id(session, incident_id):
        return state["incidents"].get(incident_id)

    monkeypatch.setattr(
        proactive_service.incidents_repository,
        "list_incidents_by_severity_since",
        fake_list_incidents_by_severity_since,
    )
    monkeypatch.setattr(
        proactive_service.incidents_repository, "get_incident_by_id", fake_get_incident_by_id
    )

    async def fake_get_document_by_id(session, document_id):
        return state["documents"].get(document_id)

    monkeypatch.setattr(
        proactive_service.knowledge_repository, "get_document_by_id", fake_get_document_by_id
    )

    async def fake_list_active_edges_by_relationship_type(
        session, *, organization_id, relationship_type
    ):
        return [
            e
            for e in state["edges"]
            if e.organization_id == organization_id and relationship_type == "documents"
        ]

    monkeypatch.setattr(
        proactive_service.graph_repository,
        "list_active_edges_by_relationship_type",
        fake_list_active_edges_by_relationship_type,
    )

    async def fake_get_finding_by_fingerprint(session, *, organization_id, fingerprint):
        for row in state["findings"].values():
            if row.organization_id == organization_id and row.fingerprint == fingerprint:
                return row
        return None

    async def fake_get_finding(session, finding_id, *, organization_id):
        row = state["findings"].get(finding_id)
        if row is not None and row.organization_id == organization_id:
            return row
        return None

    async def fake_list_findings(session, *, organization_id, status, finding_type, limit, offset):
        rows = [row for row in state["findings"].values() if row.organization_id == organization_id]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        if finding_type is not None:
            rows = [row for row in rows if row.finding_type == finding_type]
        rows.sort(key=lambda row: row.last_seen_at, reverse=True)
        return rows[offset : offset + limit]

    async def fake_list_active_findings_by_type(session, *, organization_id, finding_type):
        return [
            row
            for row in state["findings"].values()
            if row.organization_id == organization_id
            and row.finding_type == finding_type
            and row.status == "active"
        ]

    async def fake_list_evidence(session, finding_id):
        return list(state["evidence"].get(finding_id, []))

    async def fake_upsert_finding(
        session,
        *,
        organization_id,
        project_id,
        finding_type,
        fingerprint,
        title,
        summary,
        support_count,
        detector_name,
        seen_at,
    ):
        existing = await fake_get_finding_by_fingerprint(
            session, organization_id=organization_id, fingerprint=fingerprint
        )
        if existing is None:
            row = _FindingRow(
                organization_id=organization_id,
                project_id=project_id,
                finding_type=finding_type,
                fingerprint=fingerprint,
                title=title,
                summary=summary,
                support_count=support_count,
                detector_name=detector_name,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                status="active",
            )
            state["findings"][row.id] = row
            return row, "created"

        was_inactive = existing.status != "active"
        unchanged = (
            not was_inactive
            and existing.title == title
            and existing.summary == summary
            and existing.support_count == support_count
        )
        if unchanged:
            return existing, "unchanged"

        existing.status = "active"
        existing.project_id = project_id
        existing.title = title
        existing.summary = summary
        existing.support_count = support_count
        existing.detector_name = detector_name
        existing.last_seen_at = seen_at
        if was_inactive:
            existing.deactivated_at = None
        return existing, ("reactivated" if was_inactive else "updated")

    async def fake_update_support(session, finding_id, *, support_count, seen_at):
        row = state["findings"].get(finding_id)
        if row is None:
            return 0
        row.support_count = support_count
        row.last_seen_at = seen_at
        return 1

    async def fake_deactivate_finding(session, finding_id, *, deactivated_at):
        row = state["findings"].get(finding_id)
        if row is not None and row.status == "active":
            row.status = "inactive"
            row.deactivated_at = deactivated_at
            return 1
        return 0

    async def fake_replace_evidence(session, finding_id, evidence):
        state["evidence"][finding_id] = [
            _EvidenceRow(finding_id=finding_id, entity_type=et, entity_id=eid, role=role)
            for et, eid, role in evidence
        ]

    async def fake_remove_evidence_for_entity(session, *, entity_type, entity_id):
        touched = []
        for finding_id, rows in state["evidence"].items():
            remaining = [
                row
                for row in rows
                if not (row.entity_type == entity_type and row.entity_id == entity_id)
            ]
            if len(remaining) != len(rows):
                touched.append(finding_id)
            state["evidence"][finding_id] = remaining
        return touched

    monkeypatch.setattr(
        proactive_service.repository, "get_finding_by_fingerprint", fake_get_finding_by_fingerprint
    )
    monkeypatch.setattr(proactive_service.repository, "get_finding", fake_get_finding)
    monkeypatch.setattr(proactive_service.repository, "list_findings", fake_list_findings)
    monkeypatch.setattr(
        proactive_service.repository,
        "list_active_findings_by_type",
        fake_list_active_findings_by_type,
    )
    monkeypatch.setattr(proactive_service.repository, "list_evidence", fake_list_evidence)
    monkeypatch.setattr(proactive_service.repository, "upsert_finding", fake_upsert_finding)
    monkeypatch.setattr(proactive_service.repository, "update_support", fake_update_support)
    monkeypatch.setattr(proactive_service.repository, "deactivate_finding", fake_deactivate_finding)
    monkeypatch.setattr(proactive_service.repository, "replace_evidence", fake_replace_evidence)
    monkeypatch.setattr(
        proactive_service.repository, "remove_evidence_for_entity", fake_remove_evidence_for_entity
    )

    async def fake_audit(session, actor, **kwargs):
        state["audit"].append(kwargs)

    monkeypatch.setattr(proactive_service, "record_audit_event", fake_audit)

    return state


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurring_severity_below_threshold_produces_no_candidate(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    for _ in range(2):  # threshold is 3
        world["incidents"][uuid.uuid4()] = _Incident(
            id=uuid.uuid4(), organization_id=organization_id, project_id=project_id, severity="high"
        )
    candidates = await proactive_service._detect_recurring_incident_severity(None, organization_id)
    assert candidates == []


@pytest.mark.asyncio
async def test_recurring_severity_at_threshold_produces_a_candidate_with_correct_evidence(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_ids = [uuid.uuid4() for _ in range(3)]
    for i, incident_id in enumerate(incident_ids):
        world["incidents"][incident_id] = _Incident(
            id=incident_id,
            organization_id=organization_id,
            project_id=project_id,
            severity="critical",
            created_at=datetime.now(UTC) - timedelta(days=i),
        )
    candidates = await proactive_service._detect_recurring_incident_severity(None, organization_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.support_count == 3
    assert candidate.project_id == project_id
    assert {item.entity_id for item in candidate.evidence} == set(incident_ids)
    assert all(item.role == "supporting_incident" for item in candidate.evidence)


@pytest.mark.asyncio
async def test_recurring_severity_ignores_low_severity_and_stale_incidents(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    world["incidents"][uuid.uuid4()] = _Incident(
        id=uuid.uuid4(), organization_id=organization_id, project_id=project_id, severity="low"
    )
    stale_id = uuid.uuid4()
    world["incidents"][stale_id] = _Incident(
        id=stale_id,
        organization_id=organization_id,
        project_id=project_id,
        severity="high",
        created_at=datetime.now(UTC) - timedelta(days=contract.RECURRING_SEVERITY_WINDOW_DAYS + 5),
    )
    candidates = await proactive_service._detect_recurring_incident_severity(None, organization_id)
    assert candidates == []


@pytest.mark.asyncio
async def test_multi_document_below_threshold_produces_no_candidate(world):
    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    world["edges"].append(
        _Edge(
            organization_id=organization_id,
            source_entity_id=uuid.uuid4(),
            target_entity_id=incident_id,
        )
    )
    candidates = await proactive_service._detect_incident_multi_document(None, organization_id)
    assert candidates == []


@pytest.mark.asyncio
async def test_multi_document_at_threshold_produces_a_candidate(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    for document_id in document_ids:
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    candidates = await proactive_service._detect_incident_multi_document(None, organization_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.support_count == 2
    roles = {item.role for item in candidate.evidence}
    assert roles == {"anchor_incident", "supporting_document"}
    supporting_ids = {
        item.entity_id for item in candidate.evidence if item.role == "supporting_document"
    }
    assert supporting_ids == set(document_ids)


@pytest.mark.asyncio
async def test_multi_document_drops_a_stale_edge_whose_incident_no_longer_resolves(world):
    """An edge pointing at an incident that doesn't resolve (deleted,
    wrong org) is dropped, never trusted alone -- same discipline
    `core.graph.service` uses for every edge it reads."""
    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()  # deliberately never added to world["incidents"]
    for _ in range(2):
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=uuid.uuid4(),
                target_entity_id=incident_id,
            )
        )
    candidates = await proactive_service._detect_incident_multi_document(None, organization_id)
    assert candidates == []


# --------------------------------------------------------------------------
# detection orchestration: upsert / reconcile / idempotency / isolation
# --------------------------------------------------------------------------


def _seed_recurring_severity(world, organization_id, project_id, count=3):
    ids = []
    for i in range(count):
        incident_id = uuid.uuid4()
        world["incidents"][incident_id] = _Incident(
            id=incident_id,
            organization_id=organization_id,
            project_id=project_id,
            severity="high",
            created_at=datetime.now(UTC) - timedelta(days=i),
        )
        ids.append(incident_id)
    return ids


@pytest.mark.asyncio
async def test_run_detection_creates_a_finding_for_a_new_pattern(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, organization_id, project_id)

    results = await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    assert results[0].created_count == 1
    assert len(world["findings"]) == 1
    finding = next(iter(world["findings"].values()))
    assert finding.support_count == 3
    assert finding.status == "active"


@pytest.mark.asyncio
async def test_run_detection_twice_converges_no_duplicate_findings_or_evidence(world):
    """The mandatory idempotency test: run once, run twice, verify
    convergence -- no unlimited findings or evidence rows."""
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, organization_id, project_id)

    first = await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    second = await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )

    assert first[0].created_count == 1
    assert second[0].created_count == 0
    assert second[0].unchanged_count == 1
    assert len(world["findings"]) == 1
    finding_id = next(iter(world["findings"]))
    assert len(world["evidence"][finding_id]) == 3  # not 6


@pytest.mark.asyncio
async def test_run_detection_deactivates_a_finding_whose_pattern_no_longer_qualifies(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_ids = _seed_recurring_severity(world, organization_id, project_id)
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    # Support drops below threshold: remove one qualifying incident.
    del world["incidents"][incident_ids[0]]

    results = await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    assert results[0].deactivated_count == 1
    finding = next(iter(world["findings"].values()))
    assert finding.status == "inactive"
    assert finding.deactivated_at is not None


@pytest.mark.asyncio
async def test_run_detection_reactivates_a_previously_inactive_finding(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_ids = _seed_recurring_severity(world, organization_id, project_id)
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    removed = world["incidents"].pop(incident_ids[0])
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    finding = next(iter(world["findings"].values()))
    assert finding.status == "inactive"

    world["incidents"][incident_ids[0]] = removed  # support qualifies again
    results = await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    assert results[0].reactivated_count == 1
    assert finding.status == "active"
    assert finding.deactivated_at is None


@pytest.mark.asyncio
async def test_one_detector_failure_does_not_affect_the_other_or_existing_findings(
    world, monkeypatch
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, organization_id, project_id)
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    finding_before = next(iter(world["findings"].values()))
    assert finding_before.status == "active"

    async def exploding_detector(session, organization_id):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        proactive_service._DETECTORS, "recurring_incident_severity", exploding_detector
    )

    results = await proactive_service.run_detection(
        None,
        organization_id=organization_id,
        finding_types=["recurring_incident_severity", "incident_multi_document"],
    )
    failed = next(r for r in results if r.finding_type == "recurring_incident_severity")
    ok = next(r for r in results if r.finding_type == "incident_multi_document")
    assert failed.error == "boom"
    assert ok.error is None
    # The failed detector's crash must not have deactivated the existing finding.
    assert finding_before.status == "active"


@pytest.mark.asyncio
async def test_cross_organization_incidents_never_combine_into_one_finding(world):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    _seed_recurring_severity(world, org_a, uuid.uuid4())
    _seed_recurring_severity(world, org_b, uuid.uuid4())

    results_a = await proactive_service.run_detection(
        None, organization_id=org_a, finding_types=["recurring_incident_severity"]
    )
    results_b = await proactive_service.run_detection(
        None, organization_id=org_b, finding_types=["recurring_incident_severity"]
    )
    assert results_a[0].created_count == 1
    assert results_b[0].created_count == 1
    assert len(world["findings"]) == 2
    orgs = {row.organization_id for row in world["findings"].values()}
    assert orgs == {org_a, org_b}


# --------------------------------------------------------------------------
# authorized read: tenant + permission isolation, mixed visibility
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_findings_excludes_another_organizations_findings(world):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, org_a, project_id)
    await proactive_service.run_detection(
        None, organization_id=org_a, finding_types=["recurring_incident_severity"]
    )

    actor_b = _user(org_b, projects={project_id: frozenset({"incident:read"})})
    assert await proactive_service.list_findings(None, actor_b) == []


@pytest.mark.asyncio
async def test_list_findings_requires_incident_read_on_the_finding_project(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, organization_id, project_id)
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )

    no_access = _user(organization_id, projects={project_id: frozenset()})
    has_access = _user(organization_id, projects={project_id: frozenset({"incident:read"})})

    assert await proactive_service.list_findings(None, no_access) == []
    visible = await proactive_service.list_findings(None, has_access)
    assert len(visible) == 1
    assert visible[0].support_count == 3


@pytest.mark.asyncio
async def test_get_finding_raises_not_found_rather_than_permission_denied(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    _seed_recurring_severity(world, organization_id, project_id)
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["recurring_incident_severity"]
    )
    finding_id = next(iter(world["findings"]))

    no_access = _user(organization_id, projects={project_id: frozenset()})
    with pytest.raises(NotFoundError):
        await proactive_service.get_finding(None, no_access, finding_id)


@pytest.mark.asyncio
async def test_mixed_visibility_hides_documents_the_caller_cannot_review_and_recomputes_support(
    world,
):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    published_id, proposed_id = uuid.uuid4(), uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][published_id] = _Document(
        id=published_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    world["documents"][proposed_id] = _Document(
        id=proposed_id, organization_id=organization_id, project_id=project_id, status="proposed"
    )
    for document_id in (published_id, proposed_id):
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["incident_multi_document"]
    )
    finding_id = next(iter(world["findings"]))

    reader_only = _user(organization_id, projects={project_id: frozenset({"incident:read"})})
    reviewer = _user(
        organization_id, projects={project_id: frozenset({"incident:read", "knowledge:review"})}
    )

    # Below threshold (2) once the proposed document is excluded -> hidden entirely.
    with pytest.raises(NotFoundError):
        await proactive_service.get_finding(None, reader_only, finding_id)
    assert await proactive_service.list_findings(None, reader_only) == []

    detail = await proactive_service.get_finding(None, reviewer, finding_id)
    assert detail.support_count == 2
    assert {e.entity_id for e in detail.supporting_entities} == {
        incident_id,
        published_id,
        proposed_id,
    }


@pytest.mark.asyncio
async def test_deleted_document_evidence_is_excluded_even_at_full_permission(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    live_id, deleted_id = uuid.uuid4(), uuid.uuid4()
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    world["documents"][live_id] = _Document(
        id=live_id, organization_id=organization_id, project_id=project_id, status="published"
    )
    world["documents"][deleted_id] = _Document(
        id=deleted_id,
        organization_id=organization_id,
        project_id=project_id,
        status="published",
        deleted_at=datetime.now(UTC),
    )
    for document_id in (live_id, deleted_id):
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["incident_multi_document"]
    )
    finding_id = next(iter(world["findings"]))
    full_access = _user(
        organization_id, projects={project_id: frozenset({"incident:read", "knowledge:review"})}
    )

    # Only one live document remains -> below the threshold of 2 -> hidden.
    with pytest.raises(NotFoundError):
        await proactive_service.get_finding(None, full_access, finding_id)


# --------------------------------------------------------------------------
# lifecycle: handle_evidence_entity_removed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_evidence_entity_removed_deactivates_when_support_drops_below_threshold(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    for document_id in document_ids:
        world["documents"][document_id] = _Document(
            id=document_id,
            organization_id=organization_id,
            project_id=project_id,
            status="published",
        )
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["incident_multi_document"]
    )
    finding = next(iter(world["findings"].values()))
    assert finding.status == "active"

    touched = await proactive_service.handle_evidence_entity_removed(
        None, organization_id=organization_id, entity_type="document", entity_id=document_ids[0]
    )
    assert touched == 1
    assert finding.status == "inactive"


@pytest.mark.asyncio
async def test_handle_evidence_entity_removed_updates_support_when_still_above_threshold(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    for document_id in document_ids:
        world["documents"][document_id] = _Document(
            id=document_id,
            organization_id=organization_id,
            project_id=project_id,
            status="published",
        )
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["incident_multi_document"]
    )
    finding = next(iter(world["findings"].values()))
    assert finding.support_count == 3

    touched = await proactive_service.handle_evidence_entity_removed(
        None, organization_id=organization_id, entity_type="document", entity_id=document_ids[0]
    )
    assert touched == 1
    assert finding.status == "active"
    assert finding.support_count == 2


@pytest.mark.asyncio
async def test_handle_evidence_entity_removed_is_idempotent(world):
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    world["incidents"][incident_id] = _Incident(
        id=incident_id, organization_id=organization_id, project_id=project_id
    )
    for document_id in document_ids:
        world["documents"][document_id] = _Document(
            id=document_id,
            organization_id=organization_id,
            project_id=project_id,
            status="published",
        )
        world["edges"].append(
            _Edge(
                organization_id=organization_id,
                source_entity_id=document_id,
                target_entity_id=incident_id,
            )
        )
    await proactive_service.run_detection(
        None, organization_id=organization_id, finding_types=["incident_multi_document"]
    )

    first = await proactive_service.handle_evidence_entity_removed(
        None, organization_id=organization_id, entity_type="document", entity_id=document_ids[0]
    )
    second = await proactive_service.handle_evidence_entity_removed(
        None, organization_id=organization_id, entity_type="document", entity_id=document_ids[0]
    )
    assert first == 1
    assert second == 0  # already removed -- nothing left to touch
