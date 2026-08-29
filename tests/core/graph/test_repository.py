"""Tests for `app.core.graph.repository` -- SQL shape and upsert branching.

Read queries are checked by compiling the statement and inspecting its WHERE
clause (no live Postgres in this suite -- same approach and same disclosed
limitation as `tests/core/memory/test_authorization.py`). The upsert's
create/revive/unchanged branching is checked against a capturing fake
session, the same technique `tests/core/memory` uses for its own
insert/update branches.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.graph import repository


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    return value.hex in sql.replace("-", "") or str(value) in sql


class _Edge:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.status = kwargs.get("status", "active")
        self.project_id = kwargs.get("project_id")
        self.provenance_type = kwargs.get("provenance_type")
        self.provenance_id = kwargs.get("provenance_id")
        self.created_by = kwargs.get("created_by")
        self.edge_metadata = kwargs.get("edge_metadata")


class _CapturingSession:
    def __init__(self, existing=None):
        self.statements: list[object] = []
        self.added: list[object] = []
        self._existing = existing

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _Result(self._existing)

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


class _Result:
    def __init__(self, existing):
        self._existing = existing

    def scalars(self):
        return self

    def first(self):
        return self._existing


# --- read queries: tenant scoping and status filtering ---------------------


@pytest.mark.asyncio
async def test_get_direct_edges_is_scoped_to_organization_and_active_status():
    session = _CapturingSession(existing=None)
    session.execute = _make_all_execute(session)  # get_direct_edges awaits .all() via scalars
    organization_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    await repository.get_direct_edges(
        session, organization_id=organization_id, entity_type="incident", entity_id=entity_id
    )
    sql = _compile(session.statements[0])
    where = sql.lower()
    assert _contains_uuid(sql, organization_id)
    assert _contains_uuid(sql, entity_id)
    assert "status" in where
    assert "'active'" in where


@pytest.mark.asyncio
async def test_get_direct_edges_matches_either_source_or_target_endpoint():
    """An entity can be either endpoint of a stored edge -- the query must
    check both, or a caller traversing from the target side would see
    nothing."""
    session = _CapturingSession(existing=None)
    session.execute = _make_all_execute(session)
    entity_id = uuid.uuid4()
    await repository.get_direct_edges(
        session, organization_id=uuid.uuid4(), entity_type="document", entity_id=entity_id
    )
    sql = _compile(session.statements[0]).lower()
    assert "source_entity_type" in sql
    assert "target_entity_type" in sql
    assert " or " in sql


@pytest.mark.asyncio
async def test_deactivate_edge_targets_only_active_rows():
    session = _CapturingSession(existing=None)
    session.execute = _make_rowcount_execute(session, rowcount=1)
    await repository.deactivate_edge(session, uuid.uuid4())
    sql = _compile(session.statements[0]).lower().replace(" ", "")
    assert "status='active'" in sql
    assert "status='removed'" in sql


@pytest.mark.asyncio
async def test_deactivate_edges_touching_entity_matches_either_endpoint():
    session = _CapturingSession(existing=None)
    session.execute = _make_rowcount_execute(session, rowcount=2)
    entity_id = uuid.uuid4()
    await repository.deactivate_edges_touching_entity(
        session, organization_id=uuid.uuid4(), entity_type="incident", entity_id=entity_id
    )
    sql = _compile(session.statements[0]).lower()
    assert _contains_uuid(sql, entity_id)
    assert " or " in sql


def _make_all_execute(session):
    async def execute(statement, *args, **kwargs):
        session.statements.append(statement)
        return _AllResult([])

    return execute


def _make_rowcount_execute(session, *, rowcount: int):
    async def execute(statement, *args, **kwargs):
        session.statements.append(statement)
        return _RowcountResult(rowcount)

    return execute


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _RowcountResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


# --- upsert branching --------------------------------------------------


_UPSERT_KWARGS = dict(
    organization_id=uuid.uuid4(),
    project_id=uuid.uuid4(),
    source_entity_type="document",
    source_entity_id=uuid.uuid4(),
    relationship_type="documents",
    target_entity_type="incident",
    target_entity_id=uuid.uuid4(),
    provenance_type="deterministic_extraction",
    provenance_id=uuid.uuid4(),
    created_by="agent:graph_discovery",
)


@pytest.mark.asyncio
async def test_upsert_creates_a_new_row_when_none_exists():
    session = _CapturingSession(existing=None)
    row, action = await repository.upsert_derived_edge(session, **_UPSERT_KWARGS)
    assert action == "created"
    assert session.added == [row]
    assert row.status == "active"
    assert row.source_entity_type == "document"
    assert row.target_entity_type == "incident"


@pytest.mark.asyncio
async def test_upsert_revives_a_soft_deleted_row_instead_of_duplicating_it():
    existing = _Edge(status="removed", created_by="agent:old")
    session = _CapturingSession(existing=existing)
    row, action = await repository.upsert_derived_edge(session, **_UPSERT_KWARGS)
    assert action == "revived"
    assert row is existing
    assert row.status == "active"
    assert row.created_by == "agent:graph_discovery"
    assert session.added == [], "reviving must not insert a second row"


@pytest.mark.asyncio
async def test_upsert_is_a_noop_when_the_edge_is_already_active():
    existing = _Edge(status="active", created_by="agent:original")
    session = _CapturingSession(existing=existing)
    row, action = await repository.upsert_derived_edge(session, **_UPSERT_KWARGS)
    assert action == "unchanged"
    assert row is existing
    assert row.created_by == "agent:original", "an already-active edge must not be mutated"
    assert session.added == []
