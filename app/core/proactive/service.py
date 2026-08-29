"""Public interface for core/proactive -- deterministic detectors,
detection orchestration, authorized finding resolution, and the
lifecycle-cleanup hook other modules call.

Owned by: core/proactive. Business rules and authorization decisions live
here; SQL against `proactive_findings`/`proactive_finding_evidence` lives in
repository.py; SQL against every OTHER entity's own table is read directly
from that entity's existing repository module (`core.incidents.repository`,
`core.knowledge.repository`) -- there is no second copy of "what is a valid
incident" anywhere in this module, the same discipline `core.graph.service`
already established.

DETECTION IS UNSCOPED; RESOLUTION IS AUTHORIZED -- THIS IS THE WHOLE DESIGN
    `run_detection` (and the detectors it calls) reads real, current source
    state directly via repository functions, with no `Identity` and no
    permission check anywhere in that path -- the same "system-level
    maintenance pass" shape `core.graph.service.
    discover_document_incident_edges` already uses. It answers "does this
    organization's REAL data support this pattern," not "what can any one
    caller see." A finding produced this way is never handed to a caller
    directly from here.

    Every read a caller can reach (`list_findings`, `get_finding`)
    re-resolves the finding's evidence through `_resolve_evidence`, which
    re-fetches each entity from its own source table and re-applies that
    entity type's existing read gate (`incident:read`, the document
    published/`knowledge:review` rule) -- exactly `core.graph.service.
    _resolve_entity`'s invariant, restated for findings: authorization is
    part of resolution, never a post-filter, and a finding never reveals an
    entity the caller could not otherwise access.

MIXED VISIBILITY
    A finding's `support_count`, as stored, reflects the FULL (unscoped)
    detection-time count. A caller who cannot see every piece of evidence
    must never be shown that number, nor a title/summary implying it, nor
    the finding at all if what they CAN see no longer clears the finding
    type's own threshold -- see `_visible_evidence_and_recomputed_support`,
    used identically by both `list_findings` and `get_finding`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import NotFoundError, ValidationError
from app.core.graph import repository as graph_repository
from app.core.incidents import repository as incidents_repository
from app.core.knowledge import repository as knowledge_repository
from app.core.proactive import contract, repository
from app.core.proactive.schemas import (
    CandidateEvidence,
    CandidateFinding,
    FindingDetail,
    FindingEvidenceRef,
    ProactiveFinding,
    ReconciliationResult,
)
from app.database.models.pattern_models import ProactiveFinding as ProactiveFindingRow
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_INCIDENT_READ_PERMISSION = "incident:read"
_REVIEW_PERMISSION = "knowledge:review"
_DETECTOR_AGENT_NAME = "pattern_detection_agent"


def _fingerprint(finding_type: str, *canonical_key_parts: str) -> str:
    """Stable logical identity: `finding_type` plus the canonical grouping
    key, nothing else. Never a timestamp, a random id, or set-iteration
    order -- see `core.proactive.contract`'s module docstring for why each
    detector's key parts are chosen the way they are.
    """
    return ":".join((finding_type, *canonical_key_parts))


# --------------------------------------------------------------------------
# detectors -- pure functions: real source state in, candidates out.
# No persistence happens here; see `_run_one_detector`.
# --------------------------------------------------------------------------


async def _detect_recurring_incident_severity(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[CandidateFinding]:
    """Trigger: at least `minimum_support` incidents of a qualifying
    severity were created in the SAME project within a rolling window.

    Candidate discovery is one bounded, indexed query
    (`ix_incidents_org_severity` covers the equality half) -- never a scan
    compared against every other incident.
    """
    spec = contract.get_finding_spec("recurring_incident_severity")
    since = datetime.now(UTC) - timedelta(days=contract.RECURRING_SEVERITY_WINDOW_DAYS)
    incidents = await incidents_repository.list_incidents_by_severity_since(
        session,
        organization_id,
        severities=contract.RECURRING_SEVERITY_QUALIFYING_VALUES,
        since=since,
    )

    by_project: dict[uuid.UUID, list] = {}
    for incident in incidents:
        by_project.setdefault(incident.project_id, []).append(incident)

    candidates: list[CandidateFinding] = []
    for project_id, group in by_project.items():
        if len(group) < spec.minimum_support:
            continue
        group.sort(key=lambda row: row.created_at)  # deterministic ordering
        window_start = group[0].created_at.date().isoformat()
        window_end = group[-1].created_at.date().isoformat()
        candidates.append(
            CandidateFinding(
                finding_type="recurring_incident_severity",
                organization_id=organization_id,
                project_id=project_id,
                fingerprint=_fingerprint("recurring_incident_severity", str(project_id)),
                support_count=len(group),
                title=(
                    f"{len(group)} high/critical incidents in the last "
                    f"{contract.RECURRING_SEVERITY_WINDOW_DAYS} days"
                ),
                summary=(
                    f"{len(group)} incidents of severity "
                    f"{'/'.join(contract.RECURRING_SEVERITY_QUALIFYING_VALUES)} were created in "
                    f"this project between {window_start} and {window_end}."
                ),
                evidence=[
                    CandidateEvidence(
                        entity_type="incident", entity_id=row.id, role="supporting_incident"
                    )
                    for row in group
                ],
            )
        )
    return candidates


async def _detect_incident_multi_document(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[CandidateFinding]:
    """Trigger: at least `minimum_support` documents are linked to the same
    incident via the Priority 5 graph's stored `documents` relationship.

    Candidate discovery reuses the graph's own indexed edge listing
    (`list_active_edges_by_relationship_type`) -- one bounded query per
    organization, exactly the "existing graph relationships for bounded
    candidate discovery" this priority's spec names as the preferred shape.
    The edge is used ONLY to bound which incidents to look at; the evidence
    is the resolved `incident`/`document` rows, fetched below, never the
    edge itself.
    """
    spec = contract.get_finding_spec("incident_multi_document")
    edges = await graph_repository.list_active_edges_by_relationship_type(
        session, organization_id=organization_id, relationship_type="documents"
    )

    by_incident: dict[uuid.UUID, set[uuid.UUID]] = {}
    for edge in edges:
        by_incident.setdefault(edge.target_entity_id, set()).add(edge.source_entity_id)

    candidates: list[CandidateFinding] = []
    for incident_id, document_ids in by_incident.items():
        if len(document_ids) < spec.minimum_support:
            continue
        incident = await incidents_repository.get_incident_by_id(session, incident_id)
        if incident is None or incident.organization_id != organization_id:
            # A stale edge (source deleted, or an organization mismatch that
            # should be structurally impossible) is resolved live and
            # dropped, never trusted alone -- the same discipline
            # `core.graph.service` uses for every edge it reads.
            continue

        evidence = [
            CandidateEvidence(entity_type="incident", entity_id=incident.id, role="anchor_incident")
        ]
        evidence += [
            CandidateEvidence(
                entity_type="document", entity_id=document_id, role="supporting_document"
            )
            for document_id in sorted(document_ids)  # deterministic ordering, not set order
        ]
        candidates.append(
            CandidateFinding(
                finding_type="incident_multi_document",
                organization_id=organization_id,
                project_id=incident.project_id,
                fingerprint=_fingerprint("incident_multi_document", str(incident_id)),
                support_count=len(document_ids),
                title=f"{len(document_ids)} documents linked to '{incident.title}'",
                summary=(
                    f"{len(document_ids)} documents are connected to this incident via the "
                    "knowledge graph's `documents` relationship."
                ),
                evidence=evidence,
            )
        )
    return candidates


_Detector = Callable[[AsyncSession, uuid.UUID], Coroutine[Any, Any, list[CandidateFinding]]]
_DETECTORS: dict[str, _Detector] = {
    "recurring_incident_severity": _detect_recurring_incident_severity,
    "incident_multi_document": _detect_incident_multi_document,
}


def _validate_candidate(spec: contract.FindingSpec, candidate: CandidateFinding) -> None:
    """Defense in depth: a detector's own output is re-checked against the
    contract before it can ever reach persistence, the same "reject
    unsupported types loudly" discipline `core.graph.contract.get_spec`
    applies to relationships.
    """
    if candidate.support_count < spec.minimum_support:
        raise ValidationError(
            f"candidate support_count {candidate.support_count} is below the minimum "
            f"{spec.minimum_support} for {spec.finding_type!r}",
            error_code="proactive.candidate_below_threshold",
        )
    unknown_roles = {item.role for item in candidate.evidence} - set(spec.evidence_roles)
    if unknown_roles:
        raise ValidationError(
            f"candidate evidence roles {sorted(unknown_roles)} are not declared for "
            f"{spec.finding_type!r}",
            error_code="proactive.unknown_evidence_role",
        )


async def _record_lifecycle_audit(
    session: AsyncSession,
    organization_id: uuid.UUID,
    finding: ProactiveFindingRow,
    action: str,
    *,
    support_count: int | None = None,
) -> None:
    actor = Identity.for_agent(_DETECTOR_AGENT_NAME, organization_id)
    await record_audit_event(
        session,
        actor,
        action=f"proactive.finding.{action}",
        resource_type="proactive_finding",
        resource_id=finding.id,
        # Counts and type only -- never evidence content, matching
        # `core.memory.service.create_memory`'s identical reasoning for why
        # its own audit metadata excludes the memory's text. `support_count`
        # is an explicit override, not always `finding.support_count`: a
        # caller that updated support via `repository.update_support`
        # (rather than mutating the ORM row in place) must pass the new
        # value here, since the in-memory `finding` object was never touched.
        metadata={
            "finding_type": finding.finding_type,
            "support_count": support_count if support_count is not None else finding.support_count,
            "fingerprint": finding.fingerprint,
        },
    )


# --------------------------------------------------------------------------
# detection orchestration -- detect, upsert, reconcile
# --------------------------------------------------------------------------


async def _run_one_detector(
    session: AsyncSession, *, organization_id: uuid.UUID, finding_type: str
) -> ReconciliationResult:
    start = time.monotonic()
    spec = contract.get_finding_spec(finding_type)
    detector = _DETECTORS[finding_type]

    try:
        candidates = await detector(session, organization_id)
    except Exception as exc:  # noqa: BLE001 -- one detector's failure must
        # never affect another detector's run, and must never be mistaken
        # for "no patterns exist" (this priority's explicit failure-
        # isolation requirement) -- no findings are touched below this line.
        logger.warning(
            "proactive_detector_failed",
            finding_type=finding_type,
            organization_id=str(organization_id),
            error=str(exc),
        )
        return ReconciliationResult(
            detector_name=_DETECTOR_AGENT_NAME,
            finding_type=finding_type,
            organization_id=organization_id,
            duration_seconds=time.monotonic() - start,
            error=str(exc),
        )

    now = datetime.now(UTC)
    seen_fingerprints: set[str] = set()
    created = updated = reactivated = unchanged = 0
    for candidate in candidates:
        _validate_candidate(spec, candidate)
        seen_fingerprints.add(candidate.fingerprint)

        row, action = await repository.upsert_finding(
            session,
            organization_id=organization_id,
            project_id=candidate.project_id,
            finding_type=candidate.finding_type,
            fingerprint=candidate.fingerprint,
            title=candidate.title,
            summary=candidate.summary,
            support_count=candidate.support_count,
            detector_name=f"agent:{_DETECTOR_AGENT_NAME}",
            seen_at=now,
        )
        # Replaced wholesale on every upsert (including "unchanged" title/
        # summary/support_count) so membership drift -- the same documents
        # count, but a different one replaced another -- still converges.
        await repository.replace_evidence(
            session,
            row.id,
            [(item.entity_type, item.entity_id, item.role) for item in candidate.evidence],
        )

        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        elif action == "reactivated":
            reactivated += 1
        else:
            unchanged += 1
        if action != "unchanged":
            await _record_lifecycle_audit(session, organization_id, row, action)

    deactivated = 0
    for existing in await repository.list_active_findings_by_type(
        session, organization_id=organization_id, finding_type=finding_type
    ):
        if existing.fingerprint in seen_fingerprints:
            continue
        rows_affected = await repository.deactivate_finding(
            session, existing.id, deactivated_at=now
        )
        if rows_affected:
            deactivated += 1
            await _record_lifecycle_audit(session, organization_id, existing, "deactivated")

    result = ReconciliationResult(
        detector_name=_DETECTOR_AGENT_NAME,
        finding_type=finding_type,
        organization_id=organization_id,
        candidate_count=len(candidates),
        created_count=created,
        updated_count=updated,
        reactivated_count=reactivated,
        unchanged_count=unchanged,
        deactivated_count=deactivated,
        duration_seconds=time.monotonic() - start,
    )
    logger.info(
        "proactive_detection_completed",
        detector_name=_DETECTOR_AGENT_NAME,
        finding_type=finding_type,
        organization_id=str(organization_id),
        candidate_count=result.candidate_count,
        created_count=result.created_count,
        updated_count=result.updated_count,
        reactivated_count=result.reactivated_count,
        deactivated_count=result.deactivated_count,
        duration_seconds=result.duration_seconds,
    )
    return result


async def run_detection(
    session: AsyncSession, *, organization_id: uuid.UUID, finding_types: list[str] | None = None
) -> list[ReconciliationResult]:
    """Run every (or a named subset of) detector for one organization.

    Called per-organization by the scheduled reconciliation task
    (`app.agents.workers.tasks.run_pattern_detection_task`) -- see that
    module for the background-integration decision. Also safe to call
    directly/manually for one organization (e.g. from a script), the same
    dual-invocation shape `core.graph.service.
    discover_document_incident_edges` already supports.
    """
    types = finding_types if finding_types is not None else sorted(contract.FINDING_TYPES)
    return [
        await _run_one_detector(session, organization_id=organization_id, finding_type=finding_type)
        for finding_type in types
    ]


# --------------------------------------------------------------------------
# authorized evidence resolution -- shared by list_findings and get_finding
# --------------------------------------------------------------------------


async def _resolve_evidence(
    session: AsyncSession, actor: Identity, entity_type: str, entity_id: uuid.UUID, role: str
) -> FindingEvidenceRef | None:
    """Re-fetch one evidence entity from its own source table and re-apply
    that entity type's existing read gate. Duplicated from `core.graph.
    service._resolve_incident`/`_resolve_document` on purpose (those are
    module-private) -- the same accepted "small justified duplication,
    documented, kept in sync with the one real source" tradeoff this
    codebase already takes for `_ensure_same_organization` across five
    modules; a shared resolver would need to live somewhere both `core.
    graph` and `core.proactive` may import without creating a cycle, which
    does not exist yet and is not worth inventing for two call sites.
    """
    if entity_type == "incident":
        row = await incidents_repository.get_incident_by_id(session, entity_id)
        if row is None or row.organization_id != actor.organization_id:
            return None
        if not actor.has_permission(_INCIDENT_READ_PERMISSION, project_id=row.project_id):
            return None
        return FindingEvidenceRef(
            entity_type="incident", entity_id=row.id, role=role, label=row.title
        )

    if entity_type == "document":
        row = await knowledge_repository.get_document_by_id(session, entity_id)
        if (
            row is None
            or row.organization_id != actor.organization_id
            or row.deleted_at is not None
        ):
            return None
        if row.status != "published" and not actor.has_permission(
            _REVIEW_PERMISSION, project_id=row.project_id
        ):
            return None
        return FindingEvidenceRef(
            entity_type="document", entity_id=row.id, role=role, label=row.title
        )

    return None  # unknown/unsupported evidence entity type fails closed


async def _visible_evidence_and_recomputed_support(
    session: AsyncSession, actor: Identity, finding: ProactiveFindingRow
) -> tuple[list[FindingEvidenceRef], int] | None:
    """`None` means "invisible to this actor, in full" -- either the
    finding-level scope gate failed, or what remains visible no longer
    clears the finding type's own threshold. Otherwise, the evidence this
    actor may see plus the support count recomputed from ONLY that subset
    -- never the stored, unscoped count. See module docstring's "Mixed
    visibility" section.
    """
    if not actor.has_permission(_INCIDENT_READ_PERMISSION, project_id=finding.project_id):
        return None

    spec = contract.get_finding_spec(finding.finding_type)
    evidence_rows = await repository.list_evidence(session, finding.id)

    resolved: list[FindingEvidenceRef] = []
    support = 0
    for row in evidence_rows:
        ref = await _resolve_evidence(session, actor, row.entity_type, row.entity_id, row.role)
        if ref is None:
            continue
        resolved.append(ref)
        if contract.counts_toward_support(row.role):
            support += 1

    if support < spec.minimum_support:
        return None
    return resolved, support


# --------------------------------------------------------------------------
# public read API
# --------------------------------------------------------------------------


async def list_findings(
    session: AsyncSession,
    actor: Identity,
    *,
    status: str | None = None,
    finding_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ProactiveFinding]:
    """Every finding this actor may see, most-recently-seen first.

    A returned page can contain FEWER than `limit` rows even when more
    exist for this organization: rows are fetched by the DB-level filters
    first, then each is independently authorized (and its `support_count`
    recomputed to what this actor may see) in Python, since project
    permission lives on `Identity`, not on this row -- there is no SQL
    predicate to push that into. An honest, documented tradeoff, not a bug;
    findings are a low-volume, dashboard-style read, not a hot path that
    needs precise offset pagination.
    """
    rows = await repository.list_findings(
        session,
        organization_id=actor.organization_id,
        status=status,
        finding_type=finding_type,
        limit=limit,
        offset=offset,
    )
    visible: list[ProactiveFinding] = []
    for row in rows:
        resolved = await _visible_evidence_and_recomputed_support(session, actor, row)
        if resolved is None:
            continue
        _evidence, support = resolved
        visible.append(
            ProactiveFinding.model_validate(row).model_copy(update={"support_count": support})
        )
    return visible


async def get_finding(
    session: AsyncSession, actor: Identity, finding_id: uuid.UUID
) -> FindingDetail:
    """One finding, with its authorized evidence.

    `NotFoundError`, never `PermissionDeniedError`, when the actor may not
    see it -- distinguishing the two would confirm a finding id exists in
    this organization, the same leak-prevention convention `core.graph.
    service`/`core.memory.service` already establish.
    """
    row = await repository.get_finding(session, finding_id, organization_id=actor.organization_id)
    if row is None:
        raise NotFoundError(
            "Finding not found.",
            error_code="proactive.finding_not_found",
            detail={"finding_id": str(finding_id)},
        )

    resolved = await _visible_evidence_and_recomputed_support(session, actor, row)
    if resolved is None:
        raise NotFoundError(
            "Finding not found.",
            error_code="proactive.finding_not_found",
            detail={"finding_id": str(finding_id)},
        )

    evidence_refs, support = resolved
    base = ProactiveFinding.model_validate(row).model_dump(exclude={"support_count"})
    return FindingDetail(**base, support_count=support, supporting_entities=evidence_refs)


# --------------------------------------------------------------------------
# lifecycle integration -- the physical-cleanup hook other modules call
# --------------------------------------------------------------------------


async def handle_evidence_entity_removed(
    session: AsyncSession, *, organization_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> int:
    """Physical-cleanup hook: call once another module has confirmed
    `(entity_type, entity_id)` is gone. Today's one real caller is `core.
    knowledge.service.reject_document` (the only deletion path that exists
    for either evidence entity type this module covers -- incidents have no
    deletion path in this codebase yet, the same gap Priority 5 already
    documented for the graph; query-time resolution in `_resolve_evidence`
    is what protects reads in the meantime).

    Removes the stale evidence row from every finding that referenced it,
    then recomputes each affected finding's support from what remains:
    below threshold -> deactivated; still at or above threshold -> updated
    in place with the new count. Returns the number of findings touched.
    """
    finding_ids = await repository.remove_evidence_for_entity(
        session, entity_type=entity_type, entity_id=entity_id
    )
    if not finding_ids:
        return 0

    now = datetime.now(UTC)
    touched = 0
    for finding_id in finding_ids:
        finding = await repository.get_finding(session, finding_id, organization_id=organization_id)
        if finding is None:  # cross-org finding_id -- structurally shouldn't happen, defensive only
            continue

        remaining = await repository.list_evidence(session, finding_id)
        support = sum(1 for row in remaining if contract.counts_toward_support(row.role))
        spec = contract.get_finding_spec(finding.finding_type)

        if support < spec.minimum_support:
            if finding.status == "active":
                await repository.deactivate_finding(session, finding_id, deactivated_at=now)
                await _record_lifecycle_audit(session, organization_id, finding, "deactivated")
                touched += 1
        elif finding.support_count != support:
            await repository.update_support(session, finding_id, support_count=support, seen_at=now)
            await _record_lifecycle_audit(
                session, organization_id, finding, "updated", support_count=support
            )
            touched += 1

    return touched
