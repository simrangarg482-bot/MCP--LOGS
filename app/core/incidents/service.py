"""Public interface for core/incidents -- incident CRUD, timeline, and the
postmortem system of record (PROJECT_PLAN.md section 9.4).

Owned by: core/incidents. Depends on core/users (`require_permission`),
core/audit (`record_audit_event`), and core/tenancy (`get_default_project`,
for defaulting `IncidentCreate.project_id` when omitted) -- the last of
these is a new, flagged dependency edge in the same spirit as the
`core/tenancy -> core/users` edge added for role-name resolution
(ENGINEERING_DECISIONS.md's SSO-provisioning-policy entry): §9.4's own
dependency list only names core/users, core/audit, database, shared, but
resolving "which project" for an org that never bothered creating a second
one genuinely belongs to core/tenancy, not a duplicated query here.

Tenant isolation: every function takes `organization_id` and the calling
`actor: Identity`, verified via `_ensure_same_organization` before anything
else -- the same guard already used in core/tenancy/service.py. Incident and
postmortem mutations additionally pass `project_id` into
`require_permission`, making this the first real caller of the project-scoped
authorization tier (`Identity.project_permissions`, PROJECT_PLAN.md section
3.6) built earlier but never previously exercised.

Two decisions worth stating plainly rather than leaving implicit:
  - Postmortem status transitions are not fully pinned down anywhere in the
    docs (a four-value `draft`/`in_review`/`approved`/`published` enum, but
    only one explicit gate endpoint, `/postmortems/{id}/approve`).
    `approve_postmortem` here produces `"approved"`, matching the endpoint's
    name literally. `"published"` is left as a state some later, not-yet-built
    pipeline (e.g. tied to the Knowledge Gap Agent / runbook proposal flow,
    ARCHITECTURE.md section 5) would drive -- core/incidents does not trigger
    it itself.
  - `postmortem:write` (gating `update_postmortem`) is a permission code this
    migration introduces; only `postmortem:approve` was named as an example
    in DATABASE_DESIGN.md. Editing a draft and approving it are different
    privilege levels in spirit (least privilege, PROJECT_PLAN.md section
    12.8), so they get different codes rather than reusing one for both.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.incidents import repository
from app.core.incidents.reads import (
    list_incidents_for_ingestion as list_incidents_for_ingestion,
    list_postmortems_for_ingestion as list_postmortems_for_ingestion,
)
from app.core.incidents.schemas import (
    ActionItem,
    Incident,
    IncidentCreate,
    IncidentFilter,
    IncidentUpdate,
    Postmortem,
    PostmortemUpdate,
    TimelineEntry,
    TimelineNoteCreate,
)
from app.core.tenancy import service as tenancy_service
from app.core.users.service import require_permission, require_project_permission
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity, InvestigationResult

logger = get_logger(__name__)

_INCIDENT_READ_PERMISSION = "incident:read"
_INCIDENT_WRITE_PERMISSION = "incident:write"
_POSTMORTEM_WRITE_PERMISSION = "postmortem:write"
_POSTMORTEM_APPROVE_PERMISSION = "postmortem:approve"


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard -- identical in spirit to
    `core.tenancy.service._ensure_same_organization`; not extracted into a
    shared helper yet (this is only the second occurrence outside that
    module; a third would make the case for promoting it to shared/).
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "incidents_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="incident.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


async def _get_owned_incident(
    session: AsyncSession, organization_id: uuid.UUID, incident_id: uuid.UUID
):
    """Fetch an incident ORM row, raising NotFoundError unless it both
    exists and belongs to `organization_id` -- a write-time tenant-isolation
    guard of the same shape as `core.tenancy.service.register_connector`'s
    `project_id` check: guessing another organization's incident id must
    never succeed, not even to discover that it exists.
    """
    row = await repository.get_incident_by_id(session, incident_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Incident not found.",
            error_code="incident.not_found",
            detail={"incident_id": str(incident_id)},
        )
    return row


async def _get_owned_postmortem(
    session: AsyncSession, organization_id: uuid.UUID, postmortem_id: uuid.UUID
):
    """Postmortem equivalent of `_get_owned_incident`."""
    row = await repository.get_postmortem_by_id(session, postmortem_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Postmortem not found.",
            error_code="postmortem.not_found",
            detail={"postmortem_id": str(postmortem_id)},
        )
    return row


# --- Incidents -----------------------------------------------------------------


async def create_incident(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: IncidentCreate,
) -> Incident:
    """Create a new incident, defaulting `project_id` to the organization's
    default project if `data.project_id` is omitted.

    Requires `actor.user_id` -- `incidents.reported_by` is a required
    reference to a `users` row, so only a `USER`-kind identity can report one.
    """
    _ensure_same_organization(actor, organization_id)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can report an incident.",
            error_code="incident.invalid_actor",
        )

    if data.project_id is not None:
        project_id = data.project_id
    else:
        default_project = await tenancy_service.get_default_project(
            session, actor, organization_id
        )
        project_id = default_project.id

    require_project_permission(actor, project_id, _INCIDENT_WRITE_PERMISSION)

    row = await repository.insert_incident(
        session,
        organization_id=organization_id,
        project_id=project_id,
        title=data.title,
        description=data.description,
        severity=data.severity,
        reported_by=actor.user_id,
    )
    await record_audit_event(
        session,
        actor,
        action="incident.create",
        resource_type="incident",
        resource_id=row.id,
        metadata={
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "severity": data.severity,
        },
    )
    return Incident.model_validate(row)


async def get_incident(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> Incident:
    """Fetch one incident. Raises NotFoundError if it doesn't exist (or
    belongs to a different organization).

    Gated by `incident:read` (project-scoped, org-level fallback) -- until
    this fix, any organization member could read any incident's full detail
    regardless of role (2026-08 audit "H4"; see
    `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #3). The
    permission-existence check happens after `_get_owned_incident` (same
    order `update_incident` already uses), so a cross-organization/
    nonexistent incident id still 404s before any permission is evaluated --
    never leaking "this id exists, you just can't read it" to a caller who
    can't see the row at all.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, row.project_id, _INCIDENT_READ_PERMISSION)
    return Incident.model_validate(row)


async def list_incidents(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, query: IncidentFilter
) -> list[Incident]:
    """Return incidents belonging to `organization_id`, filtered/paginated
    per `query` (API_DESIGN.md: `GET /incidents`).

    Gated by an org-level `incident:read` check (no `project_id` --
    `IncidentFilter` has no project scoping, so this always spans every
    project in the organization; see `get_incident`'s docstring for the
    vulnerability this closes).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _INCIDENT_READ_PERMISSION)
    rows = await repository.list_incidents(session, organization_id, query)
    return [Incident.model_validate(row) for row in rows]


async def update_incident(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    patch: IncidentUpdate,
) -> Incident:
    """Apply a partial update (API_DESIGN.md: `PATCH /incidents/{id}`).

    Not a strict state machine -- the docs don't specify legal incident
    status transitions the way they do for postmortems -- but one automatic
    bookkeeping fact is applied regardless of what the caller sends:
    transitioning *into* `resolved`/`closed` stamps `resolved_at` if it isn't
    already set, and transitioning back to `open`/`investigating` clears it,
    since `resolved_at` should always reflect the incident's current
    resolution state, not just whatever it happened to be the first time it
    was resolved.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, existing.project_id, _INCIDENT_WRITE_PERMISSION)

    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return Incident.model_validate(existing)

    new_status = fields.get("status")
    if new_status in ("resolved", "closed") and existing.resolved_at is None:
        fields["resolved_at"] = datetime.now(timezone.utc)
    elif new_status in ("open", "investigating") and existing.resolved_at is not None:
        # Only *reopening* an already-resolved incident clears the stamp.
        # Without the `is not None` guard this fired on every open/
        # investigating patch, writing `resolved_at = NULL -> NULL` and
        # listing a phantom `resolved_at` in the audit event's changed_fields.
        fields["resolved_at"] = None

    row = await repository.update_incident(session, incident_id, **fields)
    if row is None:
        raise RuntimeError("Incident disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="incident.update",
        resource_type="incident",
        resource_id=incident_id,
        metadata={"organization_id": str(organization_id), "changed_fields": list(fields.keys())},
    )
    return Incident.model_validate(row)


# --- Timeline --------------------------------------------------------------------


async def add_timeline_note(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    data: TimelineNoteCreate,
) -> TimelineEntry:
    """Add a manual, human-authored timeline note (API_DESIGN.md).

    `event_type` is always `"note"` here -- agent-authored entries
    (`event_type="investigation"`) are a separate write path,
    `record_investigation_result`, not something this human-facing function
    produces.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, existing.project_id, _INCIDENT_WRITE_PERMISSION)

    row = await repository.insert_timeline_entry(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        event_type="note",
        event_data={"note": data.note},
        actor=actor.audit_tag,
    )
    await record_audit_event(
        session,
        actor,
        action="incident.timeline_note.add",
        resource_type="incident_timeline",
        resource_id=row.id,
        metadata={"incident_id": str(incident_id)},
    )
    return TimelineEntry.model_validate(row)


async def get_timeline(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> list[TimelineEntry]:
    """Return an incident's timeline, in chronological order.

    Gated by the same `incident:read` check as `get_incident` -- a
    timeline entry can include investigation evidence and root-cause detail,
    so it must not be readable by anyone who couldn't read the incident
    itself.
    """
    _ensure_same_organization(actor, organization_id)
    incident = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, incident.project_id, _INCIDENT_READ_PERMISSION)

    rows = await repository.list_timeline_entries(session, incident_id)
    return [TimelineEntry.model_validate(row) for row in rows]


async def record_investigation_result(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    investigation: InvestigationResult,
) -> TimelineEntry:
    """Attach an Investigation Agent's result to `incident_id`'s timeline --
    the agent-authored write path `add_timeline_note`'s own docstring already
    flagged as "a separate, not-yet-built write path." Built now (Milestone 7
    task #24) because the Postmortem Agent's root-cause-extraction step
    (AGENT_WORKFLOWS.md section 2.5) needs to find "any Investigation Agent
    evidence attached to the incident" *somewhere durable* -- without this,
    that documented step would be permanently unreachable, not merely
    unimplemented. `InvestigationResult` living in `shared/schemas/` rather
    than `agents/` is exactly what makes this importable from here without
    `core` depending on `agents` -- `shared.schemas.agent_contracts`'s own
    module docstring already names "core/incidents (persisting
    `TriageResult`-shaped data into `incident_timeline`)" as an anticipated
    consumer.

    Not gated by `require_permission`: `actor` here is already whichever
    identity was authorized to trigger `agents.triage_incident` (or
    `answer_question`, for the incident-scoped low-confidence case) in the
    first place -- this only records what that already-permitted call
    produced, the same "persistence half of an already-triggered action"
    reasoning `create_postmortem` gives for skipping its own gate.

    `event_type="investigation"`, distinct from `add_timeline_note`'s
    `"note"`; `event_data` holds `evidence`/`hypotheses`/`suggested_owner_team`/
    `suggested_next_steps` as plain JSON-safe dicts (`model_dump(mode="json")`)
    -- `incident_timeline` is meant to be readable on its own
    (DATABASE_DESIGN.md), not require a join back into `agent_executions` to
    reconstruct what an investigation found.

    Priority 7 additions: `review_status`/`critique_verdict`/
    `revision_count`/`critique_issues` (`agents.investigation.critique`) are
    written the same way -- plain JSON-safe values, no schema change to this
    table. `critique_issues` holds only short structured category tags
    (e.g. `"overconfidence:hypothesis_0"`), never raw model reasoning.
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    event_data = {
        "evidence": [item.model_dump(mode="json") for item in investigation.evidence],
        "hypotheses": [h.model_dump(mode="json") for h in investigation.hypotheses],
        "suggested_owner_team": investigation.suggested_owner_team,
        "suggested_next_steps": investigation.suggested_next_steps,
        "review_status": investigation.review_status,
        "critique_verdict": investigation.critique_verdict,
        "revision_count": investigation.revision_count,
        "critique_issues": investigation.critique_issues,
    }
    row = await repository.insert_timeline_entry(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        event_type="investigation",
        event_data=event_data,
        actor=actor.audit_tag,
    )
    return TimelineEntry.model_validate(row)


# --- Postmortems -----------------------------------------------------------------


async def create_postmortem(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    *,
    root_cause: str | None,
    action_items: list[ActionItem] | None = None,
) -> Postmortem:
    """Create a postmortem draft for `incident_id`.

    Not gated by `require_permission`: this is the persistence half of
    "trigger draft generation" (PROJECT_PLAN.md section 9.4 -- core/incidents
    calls `agents.generate_postmortem`, which returns computed content only;
    core/incidents then persists it, since agents never writes to this table
    directly). `generated_by` is always `actor.audit_tag`, so whichever
    identity triggered this (a human, or eventually `agent:postmortem_agent`)
    is the exact same string recorded as having generated it.

    Raises ConflictError if a postmortem already exists for this incident --
    one postmortem per incident (DATABASE_DESIGN.md).
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    existing_postmortem = await repository.get_postmortem_by_incident_id(session, incident_id)
    if existing_postmortem is not None:
        raise ConflictError(
            "A postmortem already exists for this incident.",
            error_code="postmortem.already_exists",
            detail={"incident_id": str(incident_id)},
        )

    row = await repository.insert_postmortem(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        root_cause=root_cause,
        action_items=[item.model_dump() for item in (action_items or [])],
        generated_by=actor.audit_tag,
    )
    await record_audit_event(
        session,
        actor,
        action="postmortem.create",
        resource_type="postmortem",
        resource_id=row.id,
        metadata={"incident_id": str(incident_id)},
    )
    return Postmortem.model_validate(row)


async def trigger_postmortem_generation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
) -> Postmortem:
    """Trigger Postmortem Agent draft generation for `incident_id`
    (API_DESIGN.md: `POST /incidents/{id}/postmortem`) -- the actual glue
    `create_postmortem`'s own docstring already describes: calls
    `agents.generate_postmortem` for computed content, then persists it here
    via `create_postmortem`, since agents/ never writes to this table
    directly.

    Guards `create_postmortem` itself deliberately does not (that function
    is the reusable persistence primitive; these are specific to "a human
    just asked to generate a draft"):
      - the incident must be `resolved`/`closed` -- AGENT_WORKFLOWS.md
        section 2.5: "called after an incident is marked resolved ... this
        agent does not run automatically on every incident." This is the
        one function representing that explicit trigger action, so it's the
        right place to enforce it.
      - `actor` needs `postmortem:write` -- the same permission code that
        already gates editing a draft (`update_postmortem`); triggering the
        initial draft's creation is at least as privileged as editing one.
      - fails fast, before spending any LLM calls, if a postmortem already
        exists for this incident -- `create_postmortem` re-checks this too
        (defense in depth against a race between the two checks), so this
        is a latency optimization, not the only guard.

    The persisted row's `generated_by` is always `"agent:postmortem_agent"`
    (AGENT_WORKFLOWS.md section 2.5), not `actor.audit_tag`: `actor` is the
    human who *triggered* generation, not who *wrote* the draft's content,
    so `create_postmortem` is called here with a separate
    `Identity.for_agent("postmortem_agent", organization_id)` -- the same
    pattern `ingestion.service.run_ingestion_job` uses for its own internal,
    agent-attributed writes. The human's own role is recorded as its own
    audit event (`postmortem.generate_requested`) instead, so the audit
    trail honestly shows both facts: who asked for a draft, and that the
    agent is what actually wrote it.
    """
    _ensure_same_organization(actor, organization_id)
    incident = await _get_owned_incident(session, organization_id, incident_id)

    if incident.status not in ("resolved", "closed"):
        raise ConflictError(
            "Postmortem generation requires a resolved or closed incident.",
            error_code="postmortem.incident_not_resolved",
            detail={"incident_id": str(incident_id), "status": incident.status},
        )

    require_project_permission(actor, incident.project_id, _POSTMORTEM_WRITE_PERMISSION)

    existing_postmortem = await repository.get_postmortem_by_incident_id(session, incident_id)
    if existing_postmortem is not None:
        raise ConflictError(
            "A postmortem already exists for this incident.",
            error_code="postmortem.already_exists",
            detail={"incident_id": str(incident_id)},
        )

    # Deferred import: agents/ already depends on core/incidents at module
    # level (e.g. `agents.investigation.evidence`, `agents.service.
    # triage_incident`), so a module-level import of `agents.service` here
    # would be a circular import at load time -- the same reasoning
    # `agents.graph.build_graph` gives for its own deferred node imports.
    from app.agents.service import generate_postmortem

    root_cause, action_items = await generate_postmortem(session, incident_id, actor)

    agent_actor = Identity.for_agent("postmortem_agent", organization_id)
    postmortem = await create_postmortem(
        session,
        agent_actor,
        organization_id,
        incident_id,
        root_cause=root_cause,
        action_items=action_items,
    )

    await record_audit_event(
        session,
        actor,
        action="postmortem.generate_requested",
        resource_type="postmortem",
        resource_id=postmortem.id,
        metadata={"incident_id": str(incident_id)},
    )
    return postmortem


async def get_postmortem(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, postmortem_id: uuid.UUID
) -> Postmortem:
    """Fetch one postmortem (draft or published).

    Approved/published postmortems are readable by anyone in the
    organization; a still-draft/in-review one requires `postmortem:write`
    or `postmortem:approve` -- mirroring `core.knowledge.service.
    get_document`'s identical published-vs-proposed gate. Unlike `list_
    recent_postmortems`'s own "read-only, no gate" note just below (which
    only ever returns already-reviewed postmortems, so needs no gate), this
    function can return a draft -- and until this fix, it did so to any org
    member holding a raw postmortem id, with no permission check at all.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if row.status not in ("approved", "published"):
        incident = await repository.get_incident_by_id(session, row.incident_id)
        project_id = incident.project_id if incident is not None else None
        if not (
            actor.has_permission(_POSTMORTEM_WRITE_PERMISSION, project_id=project_id)
            or actor.has_permission(_POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)
        ):
            raise PermissionDeniedError(
                "This postmortem has not been reviewed yet.",
                error_code="postmortem.not_reviewed",
                detail={"postmortem_id": str(postmortem_id)},
            )
    return Postmortem.model_validate(row)


async def get_postmortem_by_incident(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> Postmortem:
    """Look up the postmortem already attached to `incident_id`.

    Raises `NotFoundError` if none exists yet -- an ordinary 404 a caller is
    expected to catch to mean "no postmortem yet, offer to generate one,"
    not a genuine error state; matching every other "fetch by id" function
    in this module (`_get_owned_incident`, `_get_owned_postmortem`), none of
    which return `None` for a not-yet-existing resource, so this doesn't
    introduce a second style.

    Closes a real, previously-flagged gap: `trigger_postmortem_generation`'s
    409 "already exists" response never told the caller the existing
    postmortem's id (only the incident id, which the caller already had) --
    there was no REST way at all to go from "I have an incident id" to "is
    there already a postmortem, and if so what's its id" without this.
    Backs `GET /incidents/{incident_id}/postmortem`.

    Applies the exact same read gate `get_postmortem` does (approved/
    published open to any org member; draft/in-review requires
    `postmortem:write` or `postmortem:approve`) -- this is the same
    resource, just looked up by a different key, so it must not be a wider
    door into the same data.
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    row = await repository.get_postmortem_by_incident_id(session, incident_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "No postmortem exists for this incident yet.",
            error_code="postmortem.not_found",
            detail={"incident_id": str(incident_id)},
        )

    if row.status not in ("approved", "published"):
        incident = await repository.get_incident_by_id(session, incident_id)
        project_id = incident.project_id if incident is not None else None
        if not (
            actor.has_permission(_POSTMORTEM_WRITE_PERMISSION, project_id=project_id)
            or actor.has_permission(_POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)
        ):
            raise PermissionDeniedError(
                "This postmortem has not been reviewed yet.",
                error_code="postmortem.not_reviewed",
                detail={"postmortem_id": str(row.id)},
            )
    return Postmortem.model_validate(row)


async def list_recent_postmortems(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    *,
    limit: int = 5,
) -> list[Postmortem]:
    """Return the most recent reviewed postmortems for `organization_id`
    (`approved`/`published` only -- an unreviewed `draft` hasn't been vetted
    for correctness, so surfacing it as investigative "existing knowledge"
    for a *different* incident could propagate an unreviewed root cause).

    Backs the Investigation Agent's "existing postmortems" evidence source
    (`agents.investigation.evidence`) -- a read-only lookup, so no
    `require_permission` gate, matching `get_incident`/`list_incidents`.
    """
    _ensure_same_organization(actor, organization_id)
    rows = await repository.list_postmortems_by_organization(
        session, organization_id, statuses=("approved", "published"), limit=limit
    )
    return [Postmortem.model_validate(row) for row in rows]


# `list_postmortems_for_ingestion`/`list_incidents_for_ingestion` live in
# `core.incidents.reads` (imported and re-exported at the top of this file),
# not here, so `ingestion.connectors.runbooks`/`ingestion.connectors.
# incidents` can each depend on a module that never imports `agents` -- see
# that module's docstring for the full reasoning.


async def update_postmortem(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    postmortem_id: uuid.UUID,
    patch: PostmortemUpdate,
) -> Postmortem:
    """Human edits during review (API_DESIGN.md: `PATCH /postmortems/{id}`).

    Raises ConflictError unless the postmortem is currently
    `draft`/`in_review` -- an approved or published postmortem is a closed
    state machine, mirroring the discipline already used for invitations
    (`core.tenancy.service.revoke_invitation`).
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if existing.status not in ("draft", "in_review"):
        raise ConflictError(
            "Only a draft or in-review postmortem can be edited.",
            error_code="postmortem.not_editable",
            detail={"status": existing.status},
        )

    incident = await repository.get_incident_by_id(session, existing.incident_id)
    project_id = incident.project_id if incident is not None else None
    require_permission(actor, _POSTMORTEM_WRITE_PERMISSION, project_id=project_id)

    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return Postmortem.model_validate(existing)

    row = await repository.update_postmortem(session, postmortem_id, **fields)
    if row is None:
        raise RuntimeError("Postmortem disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="postmortem.update",
        resource_type="postmortem",
        resource_id=postmortem_id,
        metadata={"organization_id": str(organization_id), "changed_fields": list(fields.keys())},
    )
    return Postmortem.model_validate(row)


async def approve_postmortem(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, postmortem_id: uuid.UUID
) -> Postmortem:
    """Approve a postmortem -- the mandatory human-review gate
    (API_DESIGN.md: `POST /postmortems/{id}/approve`; ARCHITECTURE.md section
    5: nothing an agent generates reaches "knowledge" without this).

    Requires `actor.user_id`: only a human can approve -- an agent approving
    its own draft would defeat the entire point of a human-review gate.
    Raises ConflictError unless the postmortem is currently
    `draft`/`in_review` (approving straight from `draft`, with no
    intermediate edit, is allowed -- a reviewer who has nothing to change
    shouldn't be forced through a no-op PATCH first).
    """
    _ensure_same_organization(actor, organization_id)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can approve a postmortem.",
            error_code="postmortem.invalid_actor",
        )

    existing = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if existing.status not in ("draft", "in_review"):
        raise ConflictError(
            "Only a draft or in-review postmortem can be approved.",
            error_code="postmortem.not_reviewable",
            detail={"status": existing.status},
        )

    incident = await repository.get_incident_by_id(session, existing.incident_id)
    project_id = incident.project_id if incident is not None else None
    require_permission(actor, _POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)

    row = await repository.update_postmortem(
        session, postmortem_id, status="approved", reviewed_by=actor.user_id
    )
    if row is None:
        raise RuntimeError("Postmortem disappeared mid-approval.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="postmortem.approve",
        resource_type="postmortem",
        resource_id=postmortem_id,
        metadata={"organization_id": str(organization_id)},
    )
    return Postmortem.model_validate(row)
