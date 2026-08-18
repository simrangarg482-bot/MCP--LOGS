"""Tests for the `resolve_user_first_organization` SECURITY DEFINER wrapper
in `app.core.users.repository.get_first_organization_id`
(`c5e2a9f4d7b3_resolve_user_first_organization_function.py`).

Same style as `tests/ingestion/test_repository.py`'s Milestone 10
RLS-bypass-function tests: no real Postgres connection is available to this
suite, so this exercises the statement/parameter shape against a fake
`AsyncSession`, not the actual bypass behavior against a live database.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.users import repository


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, return_value) -> None:
        self._return_value = return_value
        self.executed: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _FakeScalarResult(self._return_value)


@pytest.mark.asyncio
async def test_get_first_organization_id_calls_bypass_function() -> None:
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    session = _FakeSession(organization_id)

    result = await repository.get_first_organization_id(session, user_id)

    assert result == organization_id
    statement, params = session.executed[0]
    assert "resolve_user_first_organization" in statement
    assert params == {"user_id": str(user_id)}


@pytest.mark.asyncio
async def test_get_first_organization_id_returns_none_when_user_holds_no_role() -> None:
    session = _FakeSession(None)

    result = await repository.get_first_organization_id(session, uuid.uuid4())

    assert result is None
