"""Tests for `app.api.deps._extract_bearer_token` -- pure logic, no
database/FastAPI app needed -- plus `get_current_identity`'s Milestone 10
RLS wiring (`set_tenant_context`), exercised with monkeypatched
`auth_service`/`users_service`/`set_tenant_context` rather than a real
database session.
"""

from __future__ import annotations

import uuid

import pytest

from app.api import deps as deps_module
from app.api.deps import _extract_bearer_token, get_current_identity
from app.core.exceptions import PermissionDeniedError
from app.shared.schemas import ActorKind, Identity


def test_extracts_token_from_valid_header() -> None:
    assert _extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


def test_is_case_insensitive_on_bearer_prefix() -> None:
    assert _extract_bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


def test_strips_surrounding_whitespace() -> None:
    assert _extract_bearer_token("Bearer   abc.def.ghi  ") == "abc.def.ghi"


@pytest.mark.parametrize("header", [None, "", "Basic abc123", "Bearer"])
def test_raises_permission_denied_for_missing_or_malformed_header(header: str | None) -> None:
    with pytest.raises(PermissionDeniedError):
        _extract_bearer_token(header)


class _FakeSession:
    """Stand-in for `AsyncSession` -- `get_current_identity` never queries
    anything on it directly, only passes it through to
    `users_service.resolve_identity` and `set_tenant_context` (both
    monkeypatched below).
    """


@pytest.mark.asyncio
async def test_get_current_identity_sets_tenant_context_after_resolving_identity(monkeypatch) -> None:
    """Milestone 10 RLS backstop: `get_current_identity` must call
    `set_tenant_context` with the resolved identity's `organization_id`,
    after resolving that identity and before returning it -- every
    downstream query issued on this same request's session relies on this
    having already happened.
    """
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    identity = Identity(
        kind=ActorKind.USER,
        subject=str(user_id),
        organization_id=organization_id,
        user_id=user_id,
    )
    session = _FakeSession()
    tenant_context_calls: list[tuple[object, uuid.UUID]] = []

    class _FakeClaims:
        def __init__(self) -> None:
            self.user_id = user_id
            self.organization_id = organization_id

    def fake_verify_access_token(token: str):
        assert token == "a-valid-token"
        return _FakeClaims()

    async def fake_resolve_identity(session_arg, user_id_arg, organization_id_arg):
        assert session_arg is session
        assert user_id_arg == user_id
        assert organization_id_arg == organization_id
        return identity

    async def fake_set_tenant_context(session_arg, organization_id_arg) -> None:
        assert session_arg is session
        tenant_context_calls.append((session_arg, organization_id_arg))

    monkeypatch.setattr(deps_module.auth_service, "verify_access_token", fake_verify_access_token)
    monkeypatch.setattr(deps_module.users_service, "resolve_identity", fake_resolve_identity)
    monkeypatch.setattr(deps_module, "set_tenant_context", fake_set_tenant_context)

    result = await get_current_identity(session, authorization="Bearer a-valid-token")

    assert result is identity
    assert tenant_context_calls == [(session, organization_id)]
