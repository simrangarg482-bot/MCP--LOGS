"""Tests for `app.database.session.set_tenant_context` -- Milestone 10's
RLS session-variable wiring. Exercised against a fake `AsyncSession` that
just records what it was asked to execute, since a real Postgres connection
isn't available to this test suite.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session as session_module
from app.database.session import set_tenant_context
from app.shared.config.settings import get_settings


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


def test_build_engine_sets_a_bounded_command_timeout(monkeypatch) -> None:
    """Phase 6.1 regression: asyncpg's own `command_timeout` bounds how
    long any single query may run once connected -- distinct from (and
    previously entirely absent alongside) the connection-establishment
    timeout. Without this, a hung/slow query had no application-level
    bound at all.

    Monkeypatches `create_async_engine` itself to capture exactly what
    `_build_engine` passes it -- more robust than reverse-engineering where
    SQLAlchemy stores `connect_args` on the constructed engine object
    afterward (an internal detail, not a stable public attribute).
    """
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        # _build_engine only constructs and returns this; never calls anything on it.
        return object()

    monkeypatch.setattr(session_module, "create_async_engine", fake_create_async_engine)

    session_module._build_engine()

    assert captured["connect_args"]["command_timeout"] == 30.0
    assert captured["connect_args"]["ssl"] is True
    assert captured["pool_recycle"] == 1800


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Nothing specified -- stays secure by default (every managed
        # provider this project targets requires TLS).
        ("postgresql+asyncpg://u:p@h/db", True),
        # `.env.example`'s historical spelling, and libpq's own.
        ("postgresql+asyncpg://u:p@h/db?ssl=require", True),
        ("postgresql+asyncpg://u:p@h/db?sslmode=require", True),
        ("postgresql+asyncpg://u:p@h/db?sslmode=verify-full", True),
        # Railway private networking: a deliberate opt-out.
        ("postgresql+asyncpg://u:p@pg.railway.internal:5432/railway?sslmode=disable", False),
        ("postgresql+asyncpg://u:p@h/db?ssl=disable", False),
        ("postgresql+asyncpg://u:p@h/db?ssl=false", False),
    ],
)
def test_ssl_connect_arg_follows_the_sslmode_parameter(url: str, expected: bool) -> None:
    assert session_module._ssl_connect_arg(url) is expected


def test_normalize_database_url_strips_every_tls_and_libpq_only_param() -> None:
    normalized = session_module._normalize_database_url(
        "postgresql+asyncpg://u:p@h/db"
        "?sslmode=disable&ssl=true&channel_binding=require&application_name=ekip"
    )
    assert "sslmode" not in normalized
    assert "ssl=" not in normalized
    assert "channel_binding" not in normalized
    # A genuine asyncpg-understood parameter is left untouched.
    assert "application_name=ekip" in normalized


def test_build_engine_disables_ssl_for_sslmode_disable(monkeypatch) -> None:
    """Railway private networking: a non-SSL Postgres must not have
    `ssl=True` forced on it (it would fail the handshake outright).
    """
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session_module, "create_async_engine", fake_create_async_engine)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://u:p@pg.railway.internal:5432/railway?sslmode=disable",
    )
    get_settings.cache_clear()
    try:
        session_module._build_engine()
    finally:
        get_settings.cache_clear()

    assert captured["connect_args"]["ssl"] is False
