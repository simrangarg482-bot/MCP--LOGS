"""Proactive findings router -- authorized, read-focused discovery of
deterministic pattern-detection output (Priority 6).

Owned by: app/api. Thin pass-through to `app.core.proactive.service` -- no
business logic beyond request/response translation, the same convention
every router in this package follows (see `memory.py`/`graph.py`'s
identical notes).

WHY THIS SURFACE IS READ-ONLY
    Findings are produced entirely by scheduled/internal detection
    (`app.agents.workers.tasks.run_pattern_detection_task`) -- there is no
    `POST /insights` (or any "detect now" endpoint) here. This priority's
    own spec is explicit that a properly authorized internal trigger
    surface is not required, and this codebase has no precedent for
    exposing "run an internal detection pass" as a public endpoint; adding
    one would be new, unjustified attack surface for a feature whose entire
    value is in what it already finds on its own schedule.

AUTHORIZATION IS STRUCTURAL, NOT PARAMETRIC
    No endpoint accepts an `organization_id`. Tenancy comes entirely from
    the authenticated `Identity`, and every finding a response could ever
    mention is independently re-authorized against its own evidence's
    existing read gates by `core.proactive.service` -- see that module's
    docstring for the full invariant, including how mixed-visibility
    findings are handled (a finding whose visible evidence falls below its
    own threshold is not returned at all, never shown with a misleading
    partial count).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import CurrentIdentity, DbSession
from app.core.proactive import service as proactive_service
from app.core.proactive.contract import FindingStatus, FindingType
from app.core.proactive.schemas import FindingDetail, ProactiveFinding

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=list[ProactiveFinding])
async def list_findings(
    actor: CurrentIdentity,
    session: DbSession,
    status: FindingStatus | None = None,
    finding_type: FindingType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ProactiveFinding]:
    """Findings this caller may see, most-recently-seen first.

    A returned page can contain fewer than `limit` rows even when more
    exist for this organization -- see `core.proactive.service.
    list_findings`'s own docstring for why (per-caller authorization and
    support recomputation happen after the database page is fetched, not
    inside the query).
    """
    return await proactive_service.list_findings(
        session, actor, status=status, finding_type=finding_type, limit=limit, offset=offset
    )


@router.get("/{finding_id}", response_model=FindingDetail)
async def get_finding(
    finding_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> FindingDetail:
    """One finding, with its authorized, resolved evidence.

    A finding the caller may not (fully) see reports as not found, never as
    forbidden -- distinguishing the two would confirm a finding id exists in
    this organization, the same leak-prevention convention every other
    single-resource `GET` in this codebase already follows.
    """
    return await proactive_service.get_finding(session, actor, finding_id)
