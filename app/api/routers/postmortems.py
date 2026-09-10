"""Postmortems router (API_DESIGN.md section 1, "Postmortems").

Owned by: app/api. `POST /incidents/{id}/postmortem` wraps
`core.incidents.service.trigger_postmortem_generation` (not
`agents.generate_postmortem` directly) -- that function is the actual
persistence glue: it calls the agent, then creates the `Postmortem` row
under `Identity.for_agent("postmortem_agent", ...)`. Calling
`agents.generate_postmortem` directly from here would return computed
content that is never saved, which is not what a REST client triggering
postmortem generation expects.

`POST /postmortems/{id}/approve` also triggers a best-effort runbooks
ingestion sync (org-knowledge/investigation feedback-loop plan, priority
P1): once `core.incidents.service.approve_postmortem` succeeds, this
router looks up the caller's org's `"runbooks"` connector
(`core.tenancy.service.get_connector_by_source`) and, if one is
registered, enqueues the same `run_ingestion_job_task` `POST /tenancy/
connectors/{id}/sync` already enqueues (`app.api.routers.tenancy.
sync_connector`) -- no new ingestion mechanism, this only adds another
producer onto the existing `arq` queue. Enqueuing happens here, in the API
layer, rather than inside `core.incidents.service.approve_postmortem`,
because `ArqPool` is a FastAPI-request-scoped dependency
(`app.api.deps.get_arq_pool`) and `core/` does not depend on FastAPI-layer
types -- the same reason `sync_connector` itself enqueues at the router
layer instead of inside `core.tenancy.service`. The lookup-and-enqueue step
is entirely best-effort: no runbooks connector registered, or the lookup/
enqueue itself failing, both log and are swallowed -- approval has already
succeeded by the time this runs and must never be undone or reported as
failed because of it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import ArqPool, CurrentIdentity, DbSession
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import Postmortem, PostmortemUpdate
from app.core.tenancy import service as tenancy_service
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_RUNBOOKS_CONNECTOR_SOURCE = "runbooks"

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
    postmortem_id: uuid.UUID, actor: CurrentIdentity, session: DbSession, arq_pool: ArqPool
) -> Postmortem:
    result = await incidents_service.approve_postmortem(
        session, actor, actor.organization_id, postmortem_id
    )
    await _trigger_runbooks_ingestion(session, arq_pool, actor.organization_id)
    return result


async def _trigger_runbooks_ingestion(session, arq_pool, organization_id: uuid.UUID) -> None:
    """Best-effort: enqueue a runbooks-connector ingestion sync so an
    approved postmortem doesn't have to wait for the next hourly
    `scheduled_reconciliation` pass to become searchable via Knowledge.
    Never allowed to fail or undo the approval that already succeeded by
    the time this runs -- every failure mode here (no connector registered,
    the lookup itself raising, the enqueue call itself raising) is caught
    and logged, never re-raised.
    """
    try:
        connector = await tenancy_service.get_connector_by_source(
            session, organization_id, _RUNBOOKS_CONNECTOR_SOURCE
        )
        if connector is None:
            logger.info(
                "postmortem_approval_runbooks_sync_skipped",
                organization_id=str(organization_id),
                reason="no_runbooks_connector",
            )
            return
        await arq_pool.enqueue_job("run_ingestion_job_task", str(connector.id))
    except Exception as exc:
        logger.warning(
            "postmortem_approval_runbooks_sync_enqueue_failed",
            organization_id=str(organization_id),
            error=str(exc),
        )
