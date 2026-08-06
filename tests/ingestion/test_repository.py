"""Tests for the Milestone 10 RLS-bypass repository functions in
`app.ingestion.repository` -- `resolve_connector_config_organization_id`,
`resolve_document_organization_id`, `list_active_connector_config_ids`.

Each one issues a single raw SQL statement calling one of the
`SECURITY DEFINER` functions defined in
`d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`. No real Postgres
connection is available to this test suite, so these tests exercise the
statement/parameter shape against a fake `AsyncSession`, not the actual
bypass behavior against a live database.
"""

from __future__ import annotations

import uuid

import pytest

from app.ingestion import repository


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class _FakeSession:
    def __init__(self, return_value) -> None:
        self._return_value = return_value
        self.executed: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _FakeScalarResult(self._return_value)


@pytest.mark.asyncio
async def test_resolve_connector_config_organization_id_calls_bypass_function() -> None:
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    session = _FakeSession(organization_id)

    result = await repository.resolve_connector_config_organization_id(session, connector_config_id)

    assert result == organization_id
    statement, params = session.executed[0]
    assert "resolve_connector_config_organization" in statement
    assert params == {"config_id": connector_config_id}


@pytest.mark.asyncio
async def test_resolve_connector_config_organization_id_returns_none_when_missing() -> None:
    session = _FakeSession(None)

    result = await repository.resolve_connector_config_organization_id(session, uuid.uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_resolve_document_organization_id_calls_bypass_function() -> None:
    organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    session = _FakeSession(organization_id)

    result = await repository.resolve_document_organization_id(session, document_id)

    assert result == organization_id
    statement, params = session.executed[0]
    assert "resolve_document_organization" in statement
    assert params == {"document_id": document_id}


@pytest.mark.asyncio
async def test_list_active_connector_config_ids_calls_bypass_function() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession(ids)

    result = await repository.list_active_connector_config_ids(session)

    assert result == ids
    statement, _params = session.executed[0]
    assert "list_active_connector_config_ids" in statement
