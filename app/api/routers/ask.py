"""Ask router -- question answering and incident triage (API_DESIGN.md
section 1, "Ask / Question answering").

Owned by: app/api. Both endpoints wrap `agents/service.py` entry points
directly (not `core/incidents`), per API_DESIGN.md section 2's `agents/`
public interface -- `answer_question` and `triage_incident` are already the
exact functions that table lists as callable by REST directly.
`trigger_source` is left at its default (`"core_api"`) on both calls, since
that default already is `"core_api"` -- the parameter exists so MCP/
scheduled callers can override it, not so REST has to restate it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.agents import service as agents_service
from app.api.deps import CurrentIdentity, DbSession
from app.shared.schemas import AskResponse

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    query: str
    incident_id: uuid.UUID | None = None


@router.post("/ask", response_model=AskResponse)
async def ask_question(data: AskRequest, actor: CurrentIdentity, session: DbSession) -> AskResponse:
    return await agents_service.answer_question(session, data.query, data.incident_id, actor)


@router.post("/incidents/{incident_id}/investigate", response_model=AskResponse)
async def investigate_incident(
    incident_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> AskResponse:
    """Triage an incident directly via the Investigation Agent
    (`agents.triage_incident`), bypassing the confidence-routed
    answer/investigation split `POST /ask` uses. API_DESIGN.md doesn't give
    this its own REST path (only the MCP `investigate_incident` tool
    contract) -- but `agents.triage_incident` is already a distinct public
    entry point with no REST route to reach it, which this fills.
    """
    return await agents_service.triage_incident(session, incident_id, actor)
