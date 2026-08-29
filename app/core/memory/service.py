"""Public interface for core/memory.

Owned by: core/memory. Business rules and authorization decisions live here;
their SQL enforcement lives in repository.py; HTTP concerns live in app/api.

Transaction model: the session is a parameter, not created here -- the same
convention every other `core/*` service follows.

AUTHORIZATION MODEL, AND WHY IT REUSES WHAT ALREADY EXISTS
    No new permission code is introduced. Two existing mechanisms carry the
    whole model:

    - Tenancy comes from `actor.organization_id`, never from an argument a
      caller supplies. Every read and write is scoped by it.
    - Project visibility comes from `Identity.project_permissions`, which
      `core.users.service.resolve_identity` already populates from
      `project_memberships` (joined through `projects` to the organization).
      A non-empty entry for a project means this person holds a membership
      there, which is exactly the question "may they see this project's
      shared memory?".

    That rule is deliberately CONSERVATIVE: an organization-level admin who
    holds no explicit project membership sees no project-scoped memory. That
    is the safe direction to be wrong in -- memory can contain a person's
    private working notes, and `Identity.has_permission`'s normal
    org-level fallback (correct for permission checks) would here widen
    visibility rather than narrow it. Failing closed is the right default;
    widening it later is additive and reversible, whereas having leaked is
    neither.

WHAT NO LLM IS REQUIRED FOR
    Nothing on the default path. Memory is created explicitly, and embedding
    uses `app.retrieval.embedding`, which runs a local sentence-transformers
    model -- no paid API, so the whole subsystem is testable and CI-safe.
    Automatic LLM-based extraction of memories from conversations is
    deliberately not implemented; see `docs/AGENT_MEMORY.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.memory import repository
from app.core.memory.schemas import Memory, MemoryCreate, MemoryUpdate, RecalledMemory
from app.retrieval import embedding
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import Identity

logger = get_logger(__name__)


def _allowed_project_ids(actor: Identity) -> list[uuid.UUID]:
    """Projects whose shared memory this actor may see -- see the module
    docstring for why this is membership-based rather than falling back to
    org-level permissions."""
    return list(actor.project_permissions.keys())


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard, matching the copies in
    `core.tenancy`/`core.incidents`/`core.knowledge`/`core.audit`."""
    if actor.organization_id != organization_id:
        logger.warning(
            "memory_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="memory.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------


async def create_memory(
    session: AsyncSession, actor: Identity, data: MemoryCreate
) -> Memory:
    """Remember something explicitly.

    `scope="user"` derives its owner from `actor.user_id` -- a caller cannot
    create a private memory *on behalf of* someone else, because there is no
    parameter through which to name them. `scope="project"` requires that the
    actor already be able to see that project's memory, which is the same
    predicate used for reading it: you may not write into a project you
    cannot read.

    When `supersedes_memory_id` is set, the older memory is marked
    `"superseded"` in the same transaction, so the replacement and the
    retirement either both land or neither does. A caller may only supersede
    a memory they can currently see.
    """
    organization_id = actor.organization_id

    owner_user_id: uuid.UUID | None = None
    if data.scope == "user":
        if actor.user_id is None:
            # An agent/service identity has no `users` row to own anything.
            raise ValidationError(
                "Only a user identity can create user-scoped memory.",
                error_code="memory.owner_required",
                detail={"scope": data.scope},
            )
        owner_user_id = actor.user_id
    elif data.scope == "project":
        assert data.project_id is not None  # guaranteed by MemoryCreate's validator
        if data.project_id not in _allowed_project_ids(actor):
            raise PermissionDeniedError(
                "You do not have access to this project.",
                error_code="memory.project_access_denied",
                detail={"project_id": str(data.project_id)},
            )

    if data.supersedes_memory_id is not None:
        existing = await repository.get_visible(
            session,
            data.supersedes_memory_id,
            organization_id=organization_id,
            actor_user_id=actor.user_id,
            allowed_project_ids=_allowed_project_ids(actor),
        )
        if existing is None:
            raise NotFoundError(
                "Memory to supersede not found.",
                error_code="memory.not_found",
                detail={"memory_id": str(data.supersedes_memory_id)},
            )

    vector = await embedding.embed_query(data.content)

    row = await repository.insert(
        session,
        organization_id=organization_id,
        scope=data.scope,
        owner_user_id=owner_user_id,
        project_id=data.project_id,
        memory_type=data.memory_type,
        content=data.content,
        embedding=vector,
        source_type=data.source_type,
        source_id=data.source_id,
        created_by=actor.audit_tag,
        supersedes_memory_id=data.supersedes_memory_id,
        memory_metadata=data.memory_metadata,
    )

    if data.supersedes_memory_id is not None:
        await repository.mark_superseded(session, data.supersedes_memory_id)

    await record_audit_event(
        session,
        actor,
        action="memory.create",
        resource_type="agent_memory",
        resource_id=row.id,
        # Scope/type/provenance only -- never the memory's content. A
        # user-private memory's text must not be duplicated into the
        # organization-readable audit trail (`audit:read` is an org-level
        # permission), which would defeat the privacy of the scope.
        metadata={
            "scope": data.scope,
            "memory_type": data.memory_type,
            "source_type": data.source_type,
            "supersedes": str(data.supersedes_memory_id) if data.supersedes_memory_id else None,
        },
    )
    logger.info(
        "memory_created",
        memory_id=str(row.id),
        scope=data.scope,
        memory_type=data.memory_type,
        organization_id=str(organization_id),
        actor=actor.audit_tag,
        content_length=len(data.content),  # length, never content
    )
    return Memory.model_validate(row)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


async def list_memories(
    session: AsyncSession, actor: Identity, *, limit: int = 50, offset: int = 0
) -> list[Memory]:
    """Every memory this actor may see, newest first."""
    rows = await repository.list_visible(
        session,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        allowed_project_ids=_allowed_project_ids(actor),
        limit=limit,
        offset=offset,
    )
    return [Memory.model_validate(row) for row in rows]


async def get_memory(session: AsyncSession, actor: Identity, memory_id: uuid.UUID) -> Memory:
    """One memory, or `NotFoundError` if this actor may not see it.

    Not-found rather than permission-denied on purpose: distinguishing the
    two would confirm that a given memory id exists in this organization,
    which is itself a leak about another user's private data.
    """
    row = await repository.get_visible(
        session,
        memory_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        allowed_project_ids=_allowed_project_ids(actor),
    )
    if row is None:
        raise NotFoundError(
            "Memory not found.",
            error_code="memory.not_found",
            detail={"memory_id": str(memory_id)},
        )
    return Memory.model_validate(row)


async def recall_relevant(
    session: AsyncSession, actor: Identity, query: str
) -> list[RecalledMemory]:
    """The injection path: authorized memories relevant to `query`.

    Ordering of concerns, which is the whole point:

      1. `repository.recall` filters by tenant + lifecycle + scope/ownership
         *inside the SQL*, then orders by vector distance and applies the
         configured limit. Unauthorized rows are never candidates.
      2. The relevance threshold drops weak matches, so an unrelated query
         injects nothing rather than the least-bad memory available.
      3. The character budget truncates the selection, so a long memory
         cannot silently consume the context an answer's evidence needs.

    Returns `[]` -- never raises -- when memory is disabled
    (`memory_recall_limit=0`), nothing is authorized, or nothing clears the
    threshold. Callers treat an empty list as "behave exactly as before".
    """
    settings = get_settings()
    if settings.memory_recall_limit <= 0:
        return []

    query_vector = await embedding.embed_query(query)
    candidates = await repository.recall(
        session,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        allowed_project_ids=_allowed_project_ids(actor),
        query_embedding=query_vector,
        limit=settings.memory_recall_limit,
    )

    selected: list[RecalledMemory] = []
    used_chars = 0
    for row, distance in candidates:
        relevance = 1.0 - distance
        if relevance < settings.memory_relevance_threshold:
            # Ordered by distance ascending, so everything after this is at
            # least as far away -- stop rather than continue scanning.
            break
        if used_chars + len(row.content) > settings.memory_context_char_budget:
            # Skip, don't break: a later memory may be short enough to fit,
            # and it is still more relevant than nothing.
            continue
        used_chars += len(row.content)
        selected.append(
            RecalledMemory(
                id=row.id,
                scope=row.scope,
                memory_type=row.memory_type,
                content=row.content,
                distance=distance,
            )
        )

    if selected:
        try:
            await repository.touch_last_accessed(
                session, [m.id for m in selected], accessed_at=datetime.now(UTC)
            )
        except Exception as exc:  # noqa: BLE001 -- bookkeeping must not fail a question
            logger.warning("memory_touch_failed", error=str(exc))

    logger.info(
        "memory_recalled",
        organization_id=str(actor.organization_id),
        actor=actor.audit_tag,
        candidates_considered=len(candidates),
        selected=len(selected),
        chars_used=used_chars,
    )
    return selected


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


async def update_memory(
    session: AsyncSession, actor: Identity, memory_id: uuid.UUID, data: MemoryUpdate
) -> Memory:
    """Edit a visible memory's content, re-embedding it so retrieval reflects
    the change immediately.

    Scope and ownership are immutable here by design -- see `MemoryUpdate`.
    """
    existing = await repository.get_visible(
        session,
        memory_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        allowed_project_ids=_allowed_project_ids(actor),
    )
    if existing is None:
        raise NotFoundError(
            "Memory not found.",
            error_code="memory.not_found",
            detail={"memory_id": str(memory_id)},
        )

    vector = await embedding.embed_query(data.content)
    updated = await repository.update_content(
        session,
        memory_id,
        content=data.content,
        embedding=vector,
        memory_metadata=data.memory_metadata,
    )
    if updated is None:  # pragma: no cover - fetched above in the same transaction
        raise RuntimeError("Memory disappeared mid-update.")

    await record_audit_event(
        session,
        actor,
        action="memory.update",
        resource_type="agent_memory",
        resource_id=memory_id,
        metadata={"scope": existing.scope, "content_length": len(data.content)},
    )
    logger.info("memory_updated", memory_id=str(memory_id), actor=actor.audit_tag)
    return Memory.model_validate(updated)


async def delete_memory(
    session: AsyncSession, actor: Identity, memory_id: uuid.UUID
) -> bool:
    """Delete a memory. Returns True if this call deleted it, False if it was
    already deleted.

    IDEMPOTENT, and observably so. The first call tombstones the row and
    destroys its content and embedding (`repository.soft_delete`). A second
    call finds the tombstone and returns `False` -- not an error, because
    retrying a delete is a normal thing for a client to do, and because the
    end state a caller cares about ("this is gone") is already true.

    A memory the actor cannot see is `NotFoundError`, indistinguishable from
    one that never existed -- deliberately, so this cannot be used to probe
    for other users' memory ids.
    """
    visible = await repository.get_visible(
        session,
        memory_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        allowed_project_ids=_allowed_project_ids(actor),
    )

    if visible is None:
        # Either it never existed, or it is already a tombstone, or it
        # belongs to someone else. Only the middle case is distinguishable
        # here -- and only within the actor's own organization.
        existing = await repository.get_any_status_for_owner(
            session, memory_id, organization_id=actor.organization_id
        )
        if existing is not None and existing.status == "deleted":
            logger.info(
                "memory_delete_noop", memory_id=str(memory_id), actor=actor.audit_tag
            )
            return False
        raise NotFoundError(
            "Memory not found.",
            error_code="memory.not_found",
            detail={"memory_id": str(memory_id)},
        )

    scope = visible.scope
    rows = await repository.soft_delete(session, memory_id)
    if rows == 0:  # pragma: no cover - visible implies active, so this cannot normally happen
        return False

    await record_audit_event(
        session,
        actor,
        action="memory.delete",
        resource_type="agent_memory",
        resource_id=memory_id,
        metadata={"scope": scope},
    )
    logger.info("memory_deleted", memory_id=str(memory_id), actor=actor.audit_tag)
    return True


# --------------------------------------------------------------------------
# agent-facing context assembly
# --------------------------------------------------------------------------


def format_memory_context(memories: Sequence[RecalledMemory]) -> str:
    """Render recalled memories as a labelled block for prompt injection.

    Returns `""` for an empty selection, which callers use to skip injection
    entirely and preserve pre-memory prompt behavior byte for byte.

    The wording matters. Each line is explicitly labelled as a previously
    saved note, so the model treats it as background context rather than as
    retrieved evidence -- memory is deliberately NOT citable (it never
    becomes a `ScoredChunk`, so it can never receive a `[n]` citation
    marker). See `app.agents.answer.generation` for where this lands in the
    prompt, and why it goes in the *untrusted* half of it.
    """
    if not memories:
        return ""
    lines = [f"- ({m.memory_type}) {m.content}" for m in memories]
    return "Previously saved notes for this user/project:\n" + "\n".join(lines)
