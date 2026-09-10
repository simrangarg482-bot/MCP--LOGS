"""Knowledge-review-queue router (API_DESIGN.md section 1, "Knowledge
review queue" and "Knowledge gaps").

Owned by: app/api. `/knowledge/proposed`, `/publish`, `/reject` are a thin
pass-through to `app.core.knowledge.service`. `/knowledge/gaps` (Milestone
9) is a thin pass-through to `app.agents.service.list_gap_reports` --
previously unwired because the Knowledge Gap Agent didn't exist yet; now
that it does, this closes the last of API_DESIGN.md section 1's documented
REST endpoints for this resource group.

`POST /knowledge/gaps/{gap_report_id}/dismiss` (Stage 4 of the Knowledge/
Knowledge Gaps review) is new surface beyond API_DESIGN.md's original REST
table -- the same close-out action `KnowledgeGapReport.status`'s own
docstring flagged as missing since Milestone 9 (`"dismissed"` was always
part of the column's vocabulary; nothing ever set it). A thin pass-through
to `app.agents.service.dismiss_gap_report`, same as `/gaps` above.

`POST /knowledge` (org-knowledge/investigation feedback-loop plan,
priority P3) is a second, REST-facing entry point onto the same
`knowledge_service.propose_document` flow `propose_runbook_update`
already uses (API_DESIGN.md section 3) -- proposing a document was
previously reachable only as an MCP tool contract; this closes that gap
for a human submitting knowledge directly, without introducing a second
document/repository/schema flow. Like `propose_document` itself, it has
no `knowledge:review` gate: any authenticated member of the organization
may submit a proposal (only publishing/rejecting one requires that
permission). `GET /{document_id}` and `PATCH /{document_id}` (human-review
improvements added alongside project-scoped RBAC/logout-everywhere) remain
read/edit operations on an existing proposal, not a second way to create
one.

Route-ordering note: `GET /{document_id}` is declared after `/proposed`,
`/gaps`, and `/gaps/{gap_report_id}/dismiss` so FastAPI's literal-prefix
routes are never shadowed by the `{document_id}` path parameter (a literal
path always needs to be registered before a variable one that could
otherwise swallow it). `POST /knowledge` (empty path) has no such ordering
requirement -- it can never be shadowed by `/{document_id}`, since the two
differ in path *length*, not just literal-vs-variable segment content --
but it is declared alongside `GET /knowledge` at the top of this file
regardless, for readability.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status

from app.agents import service as agents_service
from app.api.deps import CurrentIdentity, DbSession
from app.core.knowledge import service as knowledge_service
from app.core.knowledge.schemas import Document, DocumentProposalCreate, DocumentUpdate
from app.shared.schemas import GapReport

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("", response_model=list[Document])
async def list_published_documents(
    actor: CurrentIdentity,
    session: DbSession,
    source: str | None = None,
    updated_since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Document]:
    """Browse published, already-ingested knowledge (`source="github"`/
    `"slack"`/`"manual"`/...) -- see `knowledge_service.list_published_
    documents`'s docstring for why this has no `knowledge:review` gate,
    unlike `/proposed` below.

    `limit`/`offset` (Stage 3): same bounds as `/proposed` below.
    """
    return await knowledge_service.list_published_documents(
        session,
        actor,
        actor.organization_id,
        source=source,
        updated_since=updated_since,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=Document, status_code=status.HTTP_201_CREATED)
async def propose_document(
    data: DocumentProposalCreate, actor: CurrentIdentity, session: DbSession
) -> Document:
    """Submit a new document proposal (`status="proposed"`) directly via
    REST -- P3's human-facing counterpart to the `propose_runbook_update`
    MCP tool. Thin pass-through to `knowledge_service.propose_document`
    (same function, same validation, same organization scoping); no new
    document/repository/schema flow. No `knowledge:review` permission
    required, matching `propose_document`'s own design.
    """
    return await knowledge_service.propose_document(session, actor, actor.organization_id, data)


@router.get("/proposed", response_model=list[Document])
async def list_proposed_documents(
    actor: CurrentIdentity,
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Document]:
    return await knowledge_service.list_proposed_documents(
        session, actor, actor.organization_id, limit=limit, offset=offset
    )


@router.get("/gaps", response_model=list[GapReport])
async def list_gap_reports(actor: CurrentIdentity, session: DbSession) -> list[GapReport]:
    return await agents_service.list_gap_reports(session, actor)


@router.post("/gaps/{gap_report_id}/dismiss", response_model=GapReport)
async def dismiss_gap_report(
    gap_report_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> GapReport:
    return await agents_service.dismiss_gap_report(session, actor, gap_report_id)


@router.get("/{document_id}", response_model=Document)
async def get_document(
    document_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Document:
    return await knowledge_service.get_document(session, actor, actor.organization_id, document_id)


@router.patch("/{document_id}", response_model=Document)
async def update_document(
    document_id: uuid.UUID, data: DocumentUpdate, actor: CurrentIdentity, session: DbSession
) -> Document:
    return await knowledge_service.update_document(
        session, actor, actor.organization_id, document_id, data
    )


@router.post("/{document_id}/publish", response_model=Document)
async def publish_document(
    document_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Document:
    return await knowledge_service.publish_document(
        session, actor, actor.organization_id, document_id
    )


@router.post("/{document_id}/reject", response_model=Document)
async def reject_document(
    document_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Document:
    return await knowledge_service.reject_document(
        session, actor, actor.organization_id, document_id
    )
