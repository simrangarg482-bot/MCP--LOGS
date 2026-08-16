"""Incidents router -- CRUD + timeline (API_DESIGN.md section 1,
"Incidents").

Owned by: app/api. Every handler is a thin pass-through to
`core.incidents.service`; `organization_id` is always `actor.organization_id`
(never taken from the client), since `core.incidents.service`'s own
`_ensure_same_organization` guard would reject anything else -- passing the
identity's own organization keeps that check a no-op safety net rather than
something every request has to satisfy by carefully echoing a value back.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentIdentity, DbSession
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import (
    Incident,
    IncidentCreate,
    IncidentFilter,
    IncidentUpdate,
    Postmortem,
    TimelineEntry,
    TimelineNoteCreate,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.post("", response_model=Incident, status_code=status.HTTP_201_CREATED)
async def create_incident(
    data: IncidentCreate, actor: CurrentIdentity, session: DbSession
) -> Incident:
    return await incidents_service.create_incident(session, actor, actor.organization_id, data)


@router.get("/{incident_id}", response_model=Incident)
async def get_incident(
    incident_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Incident:
    return await incidents_service.get_incident(
        session, actor, actor.organization_id, incident_id
    )


@router.get("", response_model=list[Incident])
async def list_incidents(
    actor: CurrentIdentity,
    session: DbSession,
    query: Annotated[IncidentFilter, Depends()],
) -> list[Incident]:
    """List incidents, filterable/paginated per `query`.

    API_DESIGN.md's REST envelope convention calls for an `X-Total-Count`
    header on list endpoints; `core.incidents.service.list_incidents` does
    not expose a separate total-count query, so that header is omitted here
    rather than faked from `len(result)` (which is bounded by `limit` and
    would misrepresent the true total) -- a real fix needs a dedicated count
    query added to core/incidents, not something this router can paper over.
    """
    return await incidents_service.list_incidents(session, actor, actor.organization_id, query)


@router.patch("/{incident_id}", response_model=Incident)
async def update_incident(
    incident_id: uuid.UUID, patch: IncidentUpdate, actor: CurrentIdentity, session: DbSession
) -> Incident:
    return await incidents_service.update_incident(
        session, actor, actor.organization_id, incident_id, patch
    )


@router.get("/{incident_id}/timeline", response_model=list[TimelineEntry])
async def get_timeline(
    incident_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[TimelineEntry]:
    return await incidents_service.get_timeline(session, actor, actor.organization_id, incident_id)


@router.post(
    "/{incident_id}/timeline",
    response_model=TimelineEntry,
    status_code=status.HTTP_201_CREATED,
)
async def add_timeline_note(
    incident_id: uuid.UUID,
    data: TimelineNoteCreate,
    actor: CurrentIdentity,
    session: DbSession,
) -> TimelineEntry:
    return await incidents_service.add_timeline_note(
        session, actor, actor.organization_id, incident_id, data
    )


@router.get("/{incident_id}/postmortem", response_model=Postmortem)
async def get_incident_postmortem(
    incident_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Postmortem:
    """404s with `error_code="postmortem.not_found"` if no postmortem exists
    for this incident yet -- an expected, common state a frontend uses to
    decide whether to offer "generate a postmortem" or show the existing
    one, not a genuine error.
    """
    return await incidents_service.get_postmortem_by_incident(
        session, actor, actor.organization_id, incident_id
    )
