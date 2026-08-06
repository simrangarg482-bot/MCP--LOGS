"""Tests for `app.database.session.set_tenant_context` -- Milestone 10's
RLS session-variable wiring. Exercised against a fake `AsyncSession` that
just records what it was asked to execute, since a real Postgres connection
isn't available to this test suite.
"""

from __future__ import annotations

import uuid

import pytest

from app.database.session import set_tenant_context


class _FakeResult:
    pass


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement, params=None):
        # `text(...)` objects stringify back to the SQL they were built
        # from -- comparing that string is enough to confirm this used
        # `set_config(...)`, not a literal `SET LOCAL ...` string, and that
        # it was called with bound parameters rather than interpolated ones.
        self.executed.append((str(statement), params or {}))
        return _FakeResult()


@pytest.mark.asyncio
async def test_set_tenant_context_calls_set_config_with_bound_parameters() -> None:
    session = _FakeSession()
    organization_id = uuid.uuid4()

    await set_tenant_context(session, organization_id)

    assert len(session.executed) == 1
    statement, params = session.executed[0]
    assert "set_config" in statement
    # Not a literal `SET LOCAL app.current_organization_id = '<uuid>'`
    # string built by interpolation -- both values must arrive as bound
    # parameters, matching this function's own docstring.
    assert "app.current_organization_id" not in statement
    assert str(organization_id) not in statement
    assert params == {"guc_name": "app.current_organization_id", "org_id": str(organization_id)}


@pytest.mark.asyncio
async def test_set_tenant_context_stringifies_the_uuid() -> None:
    """`set_config`'s second argument is a Postgres `text` parameter -- the
    UUID must be passed as its string form, not the raw `uuid.UUID` object
    (which asyncpg would reject for a `text`-typed function argument).
    """
    session = _FakeSession()
    organization_id = uuid.uuid4()

    await set_tenant_context(session, organization_id)

    _statement, params = session.executed[0]
    assert isinstance(params["org_id"], str)
    assert params["org_id"] == str(organization_id)
