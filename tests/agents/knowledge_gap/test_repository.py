"""Tests for `app.agents.knowledge_gap.repository.update_gap_report_status`
-- Stage 4 of the Knowledge/Knowledge Gaps review.

Same technique as `tests/core/knowledge/test_repository.py`: a capturing
fake session plus compiled-SQL inspection for the query-scoping proof (no
live Postgres in this suite), plus a plain row-mutation check for the
found/not-found branching.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.knowledge_gap import repository


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    return value.hex in sql.replace("-", "") or str(value) in sql


class _Row:
    def __init__(self, *, id: uuid.UUID, organization_id: uuid.UUID, status: str = "open") -> None:
        self.id = id
        self.organization_id = organization_id
        self.status = status


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row=None) -> None:
        self._row = row
        self.statements: list[object] = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _ScalarResult(self._row)

    async def flush(self) -> None:
        return None

    async def refresh(self, row) -> None:
        return None


@pytest.mark.asyncio
async def test_update_gap_report_status_scopes_query_to_id_and_organization() -> None:
    """Proves the mutation is scoped, not just found by id: the compiled
    WHERE clause requires both the report's own id and the caller's
    `organization_id`, so a row belonging to another organization can never
    match -- the actual mechanism `dismiss_gap_report` relies on for
    "enforce organization ownership."
    """
    gap_report_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    session = _FakeSession(row=None)

    await repository.update_gap_report_status(
        session, gap_report_id, organization_id, status="dismissed"
    )

    where = _compile(session.statements[0])
    assert _contains_uuid(where, gap_report_id)
    assert _contains_uuid(where, organization_id)


@pytest.mark.asyncio
async def test_update_gap_report_status_returns_none_when_nothing_matches() -> None:
    """The not-found and cross-organization cases are indistinguishable at
    this layer by design (see the function's own docstring) -- both are
    just "the scoped query matched zero rows."
    """
    session = _FakeSession(row=None)

    result = await repository.update_gap_report_status(
        session, uuid.uuid4(), uuid.uuid4(), status="dismissed"
    )

    assert result is None


@pytest.mark.asyncio
async def test_update_gap_report_status_mutates_and_returns_the_matched_row() -> None:
    row = _Row(id=uuid.uuid4(), organization_id=uuid.uuid4(), status="open")
    session = _FakeSession(row=row)

    result = await repository.update_gap_report_status(
        session, row.id, row.organization_id, status="dismissed"
    )

    assert result is row
    assert result.status == "dismissed"
