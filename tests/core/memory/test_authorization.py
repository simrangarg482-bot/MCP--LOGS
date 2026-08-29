"""Tests for the memory visibility predicate -- the single most important
behavior in `app.core.memory`.

THE INVARIANT UNDER TEST
    A memory must never become visible merely because it is semantically
    similar. `repository._visibility_clause` is what enforces that, and it
    does so *inside the SQL* so unauthorized rows are never candidates for
    the vector ordering.

These compile the real statements and assert on their WHERE clauses rather
than executing them (no Postgres in this suite -- the same approach and the
same disclosed limitation as `tests/core/privacy/test_repository_scoping.py`).
That proves the predicate is present and correctly shaped; it does not prove
Postgres evaluates it as expected. The complementary service-level tests in
`test_service.py` cover the decision logic that feeds these clauses.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.memory import repository


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    """UUIDs render dash-less in literal binds for a Postgres UUID column."""
    return value.hex in sql.replace("-", "") or str(value) in sql


def _where_clause(sql: str) -> str:
    """Just the predicate, lowercased.

    Necessary because every column -- `owner_user_id` included -- also
    appears in the SELECT list, so asserting a column is *absent from the
    filter* has to look at the WHERE clause alone or it is vacuously false.
    """
    lowered = sql.lower()
    start = lowered.index("where")
    end = lowered.index("order by") if "order by" in lowered else len(lowered)
    return lowered[start:end]


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _EmptyResult()


class _EmptyResult:
    def all(self):
        return []

    def scalars(self):
        return self

    def first(self):
        return None

    def scalar_one(self):
        return 0

    rowcount = 0


_EMBEDDING = [0.1] * 384


# --- tenant isolation ------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_is_scoped_to_the_organization():
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=organization_id,
        actor_user_id=uuid.uuid4(),
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    sql = _compile(session.statements[0])
    assert "organization_id" in sql.lower()
    assert _contains_uuid(sql, organization_id)


@pytest.mark.asyncio
async def test_recall_excludes_non_active_memories():
    """Superseded and deleted memories must never be recalled."""
    session = _CapturingSession()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    where = _where_clause(_compile(session.statements[0])).replace(" ", "")
    assert "status='active'" in where


# --- private memory isolation ---------------------------------------------


@pytest.mark.asyncio
async def test_user_scoped_memory_is_filtered_to_the_requesting_user():
    """The core leak this design prevents: user A's private memory must not
    be reachable by user B, however similar the query."""
    session = _CapturingSession()
    actor_user_id = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    sql = _compile(session.statements[0])
    assert "owner_user_id" in sql.lower()
    assert _contains_uuid(sql, actor_user_id)


@pytest.mark.asyncio
async def test_another_users_id_never_appears_in_the_query():
    """Negative control for the test above: only the requesting user's id is
    bound, so there is no branch through which another user's rows could
    match."""
    session = _CapturingSession()
    actor_user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, actor_user_id)
    assert not _contains_uuid(sql, other_user_id)


@pytest.mark.asyncio
async def test_agent_identity_with_no_user_id_gets_no_user_scoped_branch():
    """An agent/service identity owns nothing, so it must not match any
    user-scoped row -- and the clause must not compare against NULL."""
    session = _CapturingSession()
    project_id = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=None,
        allowed_project_ids=[project_id],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    where = _where_clause(_compile(session.statements[0]))
    assert "owner_user_id" not in where, "an identity owning nothing must have no owner branch"
    assert "project_id" in where


# --- project scoping -------------------------------------------------------


@pytest.mark.asyncio
async def test_project_scoped_memory_is_limited_to_permitted_projects():
    session = _CapturingSession()
    allowed = uuid.uuid4()
    forbidden = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
        allowed_project_ids=[allowed],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, allowed)
    assert not _contains_uuid(sql, forbidden)


@pytest.mark.asyncio
async def test_no_eligible_scope_yields_an_impossible_predicate():
    """An identity that owns nothing and can see no project must match zero
    rows -- NOT the whole organization. Omitting the scope clause instead of
    forcing it false would be exactly that bug."""
    session = _CapturingSession()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=None,
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=5,
    )
    where = _where_clause(_compile(session.statements[0])).replace(" ", "")
    # `id IS NULL` against a NOT NULL primary key can never be true.
    assert "idisnull" in where
    assert "scope=" not in where  # no scope branch was emitted at all


# --- authorization happens before relevance -------------------------------


@pytest.mark.asyncio
async def test_authorization_and_ordering_are_in_the_same_statement():
    """The whole design: filtering and vector ordering are one query, so
    Postgres applies authorization before LIMIT selects anything. If these
    were ever split into "rank globally, then filter", this assertion is the
    tripwire."""
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    await repository.recall(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=3,
    )
    assert len(session.statements) == 1, "recall must be a single statement"
    sql = _compile(session.statements[0]).lower()
    where_pos = sql.index("where")
    order_pos = sql.index("order by")
    limit_pos = sql.index("limit")
    assert where_pos < order_pos < limit_pos, (
        "authorization must be in the WHERE clause of the same statement that "
        "orders by distance and limits -- not applied afterwards"
    )
    assert _contains_uuid(sql, organization_id)
    assert _contains_uuid(sql, actor_user_id)


@pytest.mark.asyncio
async def test_recall_respects_the_limit():
    session = _CapturingSession()
    await repository.recall(
        session,
        organization_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
        allowed_project_ids=[],
        query_embedding=_EMBEDDING,
        limit=7,
    )
    sql = _compile(session.statements[0]).lower()
    assert "limit 7" in sql


# --- listing and single-get share the same predicate ---------------------


@pytest.mark.asyncio
async def test_list_visible_uses_the_same_isolation_as_recall():
    """A listing endpoint computing visibility differently from the injection
    path would be a second, divergent authorization implementation."""
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    await repository.list_visible(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        allowed_project_ids=[],
        limit=10,
        offset=0,
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, organization_id)
    assert _contains_uuid(sql, actor_user_id)
    assert "status" in sql.lower()


@pytest.mark.asyncio
async def test_get_visible_filters_rather_than_fetching_by_primary_key():
    """Fetching by PK then checking ownership in Python is the shape that
    leaks existence; the check must be in the query."""
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    await repository.get_visible(
        session,
        memory_id,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        allowed_project_ids=[],
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, memory_id)
    assert _contains_uuid(sql, organization_id)
    assert _contains_uuid(sql, actor_user_id)


@pytest.mark.asyncio
async def test_get_any_status_is_still_tenant_scoped():
    """The idempotency helper sees tombstones, but never another tenant."""
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    await repository.get_any_status_for_owner(
        session, uuid.uuid4(), organization_id=organization_id
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, organization_id)
