"""Memory router -- explicit management of persistent agent memory
(Priority 4).

Owned by: app/api. Thin pass-through to `app.core.memory.service` -- no
business logic beyond request/response translation, matching every other
router in this package.

AUTHORIZATION IS STRUCTURAL, NOT PARAMETERIC
    No endpoint here accepts an `organization_id`, a `user_id`, or an
    `owner_user_id`. Tenancy and ownership are derived entirely from the
    authenticated `Identity`:

      - Organization: `actor.organization_id`.
      - User-scoped ownership: `actor.user_id`, so a caller cannot create or
        read memory on another person's behalf -- there is no parameter
        through which to name them.
      - Project visibility: `actor.project_permissions`, already resolved
        from `project_memberships` by `core.users.service.resolve_identity`.

    This is why there is no `GET /memories?organization_id=...`: the shape
    that would make cross-tenant access expressible simply does not exist in
    the API surface.

NO NEW PERMISSION CODE
    Memory access is governed by ownership and project membership rather
    than by a new `memory:*` permission. A person may always manage their
    own private memory; project memory follows the membership they already
    hold. Adding a permission code would mean seeding it into the catalog
    migration and granting it everywhere for no additional safety -- the
    ownership check is the real gate.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentIdentity, DbSession
from app.core.memory import service as memory_service
from app.core.memory.schemas import Memory, MemoryCreate, MemoryUpdate

router = APIRouter(prefix="/memories", tags=["memory"])


@router.post("", response_model=Memory, status_code=status.HTTP_201_CREATED)
async def create_memory(
    data: MemoryCreate, actor: CurrentIdentity, session: DbSession
) -> Memory:
    """Remember something explicitly.

    `scope="user"` stores it privately to the caller; `scope="project"`
    shares it with everyone holding a membership in the named project (which
    the caller must hold too). Set `supersedes_memory_id` to replace an
    existing memory -- the old one is retired to `"superseded"` in the same
    transaction rather than edited or dropped, preserving what was previously
    believed.
    """
    return await memory_service.create_memory(session, actor, data)


@router.get("", response_model=list[Memory])
async def list_memories(
    actor: CurrentIdentity, session: DbSession, limit: int = 50, offset: int = 0
) -> list[Memory]:
    """Every memory this caller may see, newest first.

    Uses the same visibility predicate as the agent's own recall path, so
    this listing can never show something the agent would not use, nor hide
    something it would.
    """
    return await memory_service.list_memories(session, actor, limit=limit, offset=offset)


@router.get("/{memory_id}", response_model=Memory)
async def get_memory(
    memory_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Memory:
    """One memory. A memory the caller may not see is reported as not found,
    never as forbidden -- distinguishing the two would confirm that a given
    id exists, which is itself a leak about another user's private data."""
    return await memory_service.get_memory(session, actor, memory_id)


@router.patch("/{memory_id}", response_model=Memory)
async def update_memory(
    memory_id: uuid.UUID, data: MemoryUpdate, actor: CurrentIdentity, session: DbSession
) -> Memory:
    """Edit a memory's content. Re-embeds it, so recall reflects the change
    immediately.

    Content only: `scope` and ownership are immutable, because changing them
    would be a silent privilege change (a private note quietly becoming
    project-visible). Use supersession for that instead.
    """
    return await memory_service.update_memory(session, actor, memory_id, data)


@router.delete("/{memory_id}", status_code=status.HTTP_200_OK)
async def delete_memory(
    memory_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> dict[str, bool | str]:
    """Delete a memory, destroying its content and embedding.

    A real `DELETE` verb here, unlike `DELETE /tenancy/connectors/{id}`
    (which is a status change): this genuinely renders the memory
    unrecoverable -- `status="deleted"`, `content=""`, and a zeroed embedding
    -- so nothing recallable survives. The row itself is retained as a
    tombstone purely so a repeated delete can answer honestly.

    Idempotent: `deleted=false` means it was already gone, which is a
    success, not an error.
    """
    deleted = await memory_service.delete_memory(session, actor, memory_id)
    return {
        "deleted": deleted,
        "detail": "Memory deleted." if deleted else "Memory was already deleted.",
    }
