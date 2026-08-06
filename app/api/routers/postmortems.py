"""Postmortems router (API_DESIGN.md section 1, "Postmortems").

Owned by: app/api. `POST /incidents/{id}/postmortem` wraps
`core.incidents.service.trigger_postmortem_generation` (not
`agents.generate_postmortem` directly) -- that function is the actual
persistence glue: it calls the agent, then creates the `Postmortem` row
under `Identity.for_agent("postmortem_agent", ...)`. Calling
`agents.generate_postmortem` directly from here would return computed
content that is never saved, which is not what a REST client triggering
postmortem generation expects.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentIdentity, DbSession
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import Postmortem, PostmortemUpdate

router = APIRouter(tags=["postmortems"])


@router.post(
    "/incidents/{incident_id}/postmortem",
    response_model=Postmortem,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_postmortem(
    incident_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Postmortem:
    return await incidents_service.trigger_postmortem_generation(
        session, actor, actor.organization_id, incident_id
    )


@router.get("/postmortems/{postmortem_id}", response_model=Postmortem)
async def get_postmortem(
    postmortem_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Postmortem:
    return await incidents_service.get_postmortem(
        session, actor, actor.organization_id, postmortem_id
    )


@router.patch("/postmortems/{postmortem_id}", response_model=Postmortem)
async def update_postmortem(
    postmortem_id: uuid.UUID,
    patch: PostmortemUpdate,
    actor: CurrentIdentity,
    session: DbSession,
) -> Postmortem:
    return await incidents_service.update_postmortem(
        session, actor, actor.organization_id, postmortem_id, patch
    )


@router.post("/postmortems/{postmortem_id}/approve", response_model=Postmortem)
async def approve_postmortem(
    postmortem_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Postmortem:
    return await incidents_service.approve_postmortem(
        session, actor, actor.organization_id, postmortem_id
    )
