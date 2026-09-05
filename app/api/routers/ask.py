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
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.agents import service as agents_service
from app.agents.schemas import AgentExecution
from app.api.deps import CurrentIdentity, DbSession
from app.api.rate_limit import rate_limit_by_user
from app.retrieval.schemas import CollectionName, ScoredChunk
from app.shared.schemas import AskResponse

router = APIRouter(tags=["ask"])

# Phase 6.5: per-user, not per-org or per-IP -- each real human asking
# questions should get their own budget, matching how these endpoints'
# actual cost (an LLM call per request) scales with individual usage, not
# with how many other users share an organization. `/ask`'s limit is looser
# than `/investigate`'s: a full investigation runs the heavier Investigation
# Agent graph (broader evidence gathering, more LLM calls per request), so
# a caller can reasonably ask more plain questions per minute than trigger
# full investigations.
_ASK_RATE_LIMIT = rate_limit_by_user(scope="ask.question", requests_per_minute=20)
_INVESTIGATE_RATE_LIMIT = rate_limit_by_user(scope="ask.investigate", requests_per_minute=10)
_SEARCH_RATE_LIMIT = rate_limit_by_user(scope="ask.search", requests_per_minute=30)


class AskRequest(BaseModel):
    query: str
    incident_id: uuid.UUID | None = None


@router.post("/ask", response_model=AskResponse, dependencies=[Depends(_ASK_RATE_LIMIT)])
async def ask_question(data: AskRequest, actor: CurrentIdentity, session: DbSession) -> AskResponse:
    return await agents_service.answer_question(session, data.query, data.incident_id, actor)


@router.get("/ask/history", response_model=list[AgentExecution])
async def get_ask_history(
    actor: CurrentIdentity, session: DbSession, limit: int = 20, offset: int = 0
) -> list[AgentExecution]:
    """The caller's own past `answer_question`/`triage_incident` runs, newest
    first -- `agents_service.get_question_history` requires `actor.user_id`,
    which every REST caller (never MCP/scheduled) has.
    """
    return await agents_service.get_question_history(session, actor, limit=limit, offset=offset)


@router.post(
    "/incidents/{incident_id}/investigate",
    response_model=AskResponse,
    dependencies=[Depends(_INVESTIGATE_RATE_LIMIT)],
)
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


class SimilarIncidentsRequest(BaseModel):
    description: str
    top_k: int = 10


@router.post(
    "/search/similar-incidents",
    response_model=list[ScoredChunk],
    dependencies=[Depends(_SEARCH_RATE_LIMIT)],
)
async def search_similar_incidents(
    data: SimilarIncidentsRequest, actor: CurrentIdentity, session: DbSession
) -> list[ScoredChunk]:
    """Thin wrapper -- `agents_service.search_similar_incidents` already has
    an MCP tool entry point but no REST route; this fills that gap the same
    way `POST /ask` fills it for `answer_question`.
    """
    return await agents_service.search_similar_incidents(
        session, data.description, actor, top_k=data.top_k
    )


class RecentChangesRequest(BaseModel):
    query: str
    since: datetime | None = None
    top_k: int = 10
    # Matches `agents_service.search_recent_changes`'s own default -- see
    # that function's docstring: commit/PR/issue content has no file
    # extension, so it's classified/stored under "documentation", not
    # "code" (which holds only literal source files).
    collection: CollectionName = "documentation"


@router.post(
    "/search/recent-changes",
    response_model=list[ScoredChunk],
    dependencies=[Depends(_SEARCH_RATE_LIMIT)],
)
async def search_recent_changes(
    data: RecentChangesRequest, actor: CurrentIdentity, session: DbSession
) -> list[ScoredChunk]:
    """Thin wrapper -- see `search_similar_incidents` above; same gap,
    `agents_service.search_recent_changes` side.
    """
    return await agents_service.search_recent_changes(
        session, data.query, actor, since=data.since, top_k=data.top_k, collection=data.collection
    )
