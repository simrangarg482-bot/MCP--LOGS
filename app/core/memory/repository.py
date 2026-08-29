"""Persistence for core/memory.

Owned by: core/memory. Pure data access; scope/authorization *decisions*
(which projects an actor may see) live in service.py, but their *enforcement*
is compiled into the SQL here, which is the point of this module.

THE CENTRAL INVARIANT -- AUTHORIZATION IS PART OF THE QUERY
    `recall` builds one statement whose WHERE clause carries the tenant
    filter, the lifecycle filter, and the scope/ownership filter, and only
    then orders by vector distance and applies `LIMIT`. Postgres therefore
    evaluates authorization *before* relevance ranking selects anything: an
    unauthorized row is not ranked-then-dropped, it is never a candidate.

    This is not a stylistic preference. The alternative shape --
    "nearest N by embedding, then filter" -- is wrong even when its final
    output happens to match, because the intermediate result set contains
    other users' private memories, and that set leaks through `LIMIT`
    (authorized rows silently pushed out by unauthorized nearer ones),
    through timing, through query plans, and through the next person who
    adds logging or a metric to this function. There is exactly one read
    path used for injection, and the filter is inside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.memory_models import AgentMemory


def _visibility_clause(
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed_project_ids: Sequence[uuid.UUID],
):
    """The authorization predicate every read shares.

    Three conjunctive requirements, then a disjunction over the implemented
    scopes:

      organization_id matches            -- tenant isolation
      status = 'active'                  -- excludes superseded and deleted
      AND ( user-scoped AND owned by this actor
            OR project-scoped AND in a project this actor may see )

    Note what this cannot express: there is no branch that returns a
    user-scoped row to anyone but its owner, and no branch at all for a
    scope value this module does not implement. A row with an unexpected
    `scope`, or a `"project"` row whose `project_id` went NULL (its project
    was deleted -- `ON DELETE SET NULL`), matches nothing and is therefore
    invisible. Failing closed on unrecognized data is deliberate.

    `actor_user_id is None` (an agent/service identity with no `users` row)
    yields no user-scoped branch at all rather than a branch comparing
    against NULL -- `owner_user_id = NULL` is never true in SQL, but relying
    on that would be relying on a subtlety, so the branch is simply omitted.
    """
    scope_branches = []

    if actor_user_id is not None:
        scope_branches.append(
            and_(AgentMemory.scope == "user", AgentMemory.owner_user_id == actor_user_id)
        )

    if allowed_project_ids:
        scope_branches.append(
            and_(
                AgentMemory.scope == "project",
                AgentMemory.project_id.in_(list(allowed_project_ids)),
            )
        )

    if not scope_branches:
        # No eligible scope at all: an identity that owns nothing and can see
        # no project must match zero rows. Expressed as an explicitly false
        # predicate rather than by skipping the clause -- omitting it would
        # turn "sees nothing" into "sees the entire organization".
        return and_(
            AgentMemory.organization_id == organization_id,
            AgentMemory.status == "active",
            AgentMemory.id.is_(None),  # never true; id is NOT NULL
        )

    return and_(
        AgentMemory.organization_id == organization_id,
        AgentMemory.status == "active",
        or_(*scope_branches),
    )


def _visible_select(
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed_project_ids: Sequence[uuid.UUID],
) -> Select:
    return select(AgentMemory).where(
        _visibility_clause(organization_id, actor_user_id, allowed_project_ids)
    )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


async def recall(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed_project_ids: Sequence[uuid.UUID],
    query_embedding: list[float],
    limit: int,
) -> list[tuple[AgentMemory, float]]:
    """Nearest authorized memories to `query_embedding`, closest first.

    Returns `(memory, cosine_distance)` pairs. The authorization filter is
    applied in the same statement as the ordering -- see this module's
    docstring for why that ordering of concerns is the whole design.

    `cosine_distance` comes from pgvector's `<=>` operator via
    `Vector.cosine_distance`, matching how `retrieval.pgvector.store`
    computes distance for document chunks; the service converts it to a
    `1 - distance` relevance before applying its threshold.
    """
    distance = AgentMemory.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(AgentMemory, distance)
        .where(_visibility_clause(organization_id, actor_user_id, allowed_project_ids))
        .order_by(distance.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], float(row[1])) for row in rows]


async def list_visible(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed_project_ids: Sequence[uuid.UUID],
    limit: int,
    offset: int,
) -> Sequence[AgentMemory]:
    """Newest-first listing of what this actor may see.

    Shares `_visibility_clause` with `recall` on purpose: a listing endpoint
    that computed visibility differently from the injection path would be a
    second, divergent authorization implementation -- and the more visible of
    the two, so the divergence would be found by a user rather than a test.
    """
    stmt = (
        _visible_select(organization_id, actor_user_id, allowed_project_ids)
        .order_by(AgentMemory.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return (await session.execute(stmt)).scalars().all()


async def get_visible(
    session: AsyncSession,
    memory_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed_project_ids: Sequence[uuid.UUID],
) -> AgentMemory | None:
    """One memory, but only if this actor may see it.

    Deliberately not `session.get(AgentMemory, memory_id)`: fetching by
    primary key and then checking ownership in Python is the shape that
    leaks existence (a caller learns the id is real from a different error),
    and it puts the authorization decision somewhere a future refactor can
    drop it. Here, an id the actor may not see is simply not found.
    """
    stmt = _visible_select(organization_id, actor_user_id, allowed_project_ids).where(
        AgentMemory.id == memory_id
    )
    return (await session.execute(stmt)).scalars().first()


async def get_any_status_for_owner(
    session: AsyncSession,
    memory_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
) -> AgentMemory | None:
    """Fetch a memory in ANY status, scoped only by organization.

    Used exclusively by idempotent delete: a second delete of the same id
    must be able to see the existing `"deleted"` tombstone in order to
    report "already deleted" rather than "not found". Still tenant-scoped --
    never a bare primary-key lookup.
    """
    stmt = select(AgentMemory).where(
        AgentMemory.id == memory_id, AgentMemory.organization_id == organization_id
    )
    return (await session.execute(stmt)).scalars().first()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


async def insert(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    scope: str,
    owner_user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    memory_type: str,
    content: str,
    embedding: list[float],
    source_type: str | None,
    source_id: uuid.UUID | None,
    created_by: str,
    supersedes_memory_id: uuid.UUID | None,
    memory_metadata: dict | None,
) -> AgentMemory:
    row = AgentMemory(
        organization_id=organization_id,
        scope=scope,
        owner_user_id=owner_user_id,
        project_id=project_id,
        memory_type=memory_type,
        content=content,
        embedding=embedding,
        source_type=source_type,
        source_id=source_id,
        created_by=created_by,
        status="active",
        supersedes_memory_id=supersedes_memory_id,
        memory_metadata=memory_metadata,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update_content(
    session: AsyncSession,
    memory_id: uuid.UUID,
    *,
    content: str,
    embedding: list[float],
    memory_metadata: dict | None,
) -> AgentMemory | None:
    """Replace content AND its embedding together.

    Both in one statement, never separately: a row whose `content` had been
    updated but whose `embedding` still encoded the old text would keep
    surfacing for the old topic and answer with the new words -- a stale
    memory that looks fresh. Coupling them here makes that state
    unrepresentable.
    """
    stmt = (
        update(AgentMemory)
        .where(AgentMemory.id == memory_id)
        .values(content=content, embedding=embedding, memory_metadata=memory_metadata)
        .returning(AgentMemory)
    )
    return (await session.execute(stmt)).scalars().first()


async def mark_superseded(session: AsyncSession, memory_id: uuid.UUID) -> int:
    """Flip an `"active"` memory to `"superseded"`.

    Content and embedding are intentionally KEPT (unlike delete): a
    superseded memory is provenance -- what we used to believe -- and
    `status != 'active'` already makes it unrecallable. Only genuinely
    active rows are affected, so re-running is a zero-row no-op rather than
    resurrecting or double-processing anything.
    """
    stmt = (
        update(AgentMemory)
        .where(AgentMemory.id == memory_id, AgentMemory.status == "active")
        .values(status="superseded")
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def soft_delete(session: AsyncSession, memory_id: uuid.UUID) -> int:
    """Tombstone a memory and destroy its recallable content.

    `status="deleted"` AND `content=""` AND `embedding=<zero vector>`, in one
    statement. This is the direct lesson of the Priority 3 bug
    (`docs/DATA_LIFECYCLE.md` section 5): flipping a status while leaving the
    text and vector in place is precisely how "deleted" content stays
    retrievable. Two independent barriers again, as there:

      1. `status != 'active'` excludes the row from every read path.
      2. The content and embedding are gone, so even a future query that
         forgot barrier 1 has nothing meaningful to return or rank.

    `embedding` is NOT NULL (deliberately -- see the model), so it is zeroed
    rather than nulled. A zero vector carries no information about the
    original text, which is the property that matters.

    Idempotent: the `status != 'deleted'` guard means a second call matches
    zero rows and returns 0, which the service reports as already-deleted
    rather than as an error.
    """
    stmt = (
        update(AgentMemory)
        .where(AgentMemory.id == memory_id, AgentMemory.status != "deleted")
        .values(
            status="deleted",
            content="",
            embedding=[0.0] * 384,
        )
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def touch_last_accessed(
    session: AsyncSession, memory_ids: Sequence[uuid.UUID], *, accessed_at: datetime
) -> int:
    """Record that these memories were actually injected into a context.

    Best-effort and non-essential -- the service treats a failure here as
    non-fatal, since failing a user's question because a bookkeeping
    timestamp could not be written would be the wrong trade.
    """
    if not memory_ids:
        return 0
    stmt = (
        update(AgentMemory)
        .where(AgentMemory.id.in_(list(memory_ids)))
        .values(last_accessed_at=accessed_at)
    )
    return int((await session.execute(stmt)).rowcount or 0)


# --------------------------------------------------------------------------
# privacy / data-lifecycle support (Priority 3 integration)
# --------------------------------------------------------------------------


async def count_user_scoped(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """How many user-private memories this person holds in this org, in any
    status. Counted for `core.privacy`'s deletion plan."""
    from sqlalchemy import func

    stmt = select(func.count()).select_from(AgentMemory).where(
        AgentMemory.owner_user_id == user_id,
        AgentMemory.organization_id == organization_id,
        AgentMemory.scope == "user",
    )
    return int((await session.execute(stmt)).scalar_one())


async def hard_delete_user_scoped(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Physically remove this person's user-private memories.

    A genuine `DELETE`, not a tombstone -- unlike `soft_delete`, whose
    tombstone exists to make interactive deletion idempotent and observable.
    Under a data-subject deletion request there is nobody left to observe it,
    and the correct outcome is that the rows cease to exist. Because the
    embedding is a column on the same row, removing the row removes the
    vector: no orphan is possible.

    Scoped to `scope="user"` AND `owner_user_id` AND `organization_id`.
    Project-scoped memories are deliberately untouched even if this person
    created them -- they are shared with a project, exactly as documents
    created by a departing employee remain organization knowledge
    (`docs/DATA_LIFECYCLE.md` section 4).
    """
    from sqlalchemy import delete as sql_delete

    stmt = sql_delete(AgentMemory).where(
        AgentMemory.owner_user_id == user_id,
        AgentMemory.organization_id == organization_id,
        AgentMemory.scope == "user",
    )
    return int((await session.execute(stmt)).rowcount or 0)
