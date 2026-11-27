"""Database-free checks for member offboarding: the `me` claim, the identity check, and the service's
validation rules. Real-database behaviour (RLS, the epoch trigger, revocation) is in
`test_member_offboarding_postgres.py` and `tests/api/test_member_offboarding_api_postgres.py`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.users import membership
from app.core.users import repository as users_repository
from app.core.users import service as users_service
from app.core.users.schemas import MemberStatusChange
from app.shared.schemas import ActorKind, Identity
from tests.core.auth.test_sso_sessions_provisioning import _JWT_SECRET, _settings

USER, ORG = uuid.uuid4(), uuid.uuid4()


# ---------------------------------------------------------------- the `me` claim


def test_tokens_carry_the_member_epoch_they_were_issued_under(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    token, _i, _e = auth_service._issue_access_token(USER, ORG, member_epoch=4)
    assert jose_jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])["me"] == 4
    assert auth_service.verify_access_token(token).member_epoch == 4
    default, _i, _e = auth_service._issue_access_token(USER, ORG)
    assert jose_jwt.decode(default, _JWT_SECRET, algorithms=["HS256"])["me"] == 0


def test_a_token_without_the_claim_verifies_as_member_epoch_zero(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    now = datetime.now(UTC)
    legacy = jose_jwt.encode(
        {"sub": str(USER), "organization_id": str(ORG), "type": "access", "iat": int(now.timestamp()),
         "exp": int((now + timedelta(minutes=5)).timestamp())},
        _JWT_SECRET, algorithm="HS256",
    )
    assert auth_service.verify_access_token(legacy).member_epoch == 0


@pytest.mark.parametrize("value", ["1", -1, 1.5, True, [1]])
def test_a_malformed_member_epoch_is_refused_not_defaulted(monkeypatch, value) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    now = datetime.now(UTC)
    token = jose_jwt.encode(
        {"sub": str(USER), "organization_id": str(ORG), "type": "access", "iat": int(now.timestamp()),
         "exp": int((now + timedelta(minutes=5)).timestamp()), "me": value},
        _JWT_SECRET, algorithm="HS256",
    )
    with pytest.raises(PermissionDeniedError) as info:
        auth_service.verify_access_token(token)
    assert info.value.error_code == "auth.invalid_token"


# ---------------------------------------------------------------- resolve_identity


@pytest.fixture
def identity_fakes(monkeypatch):
    state = {"deactivated": False, "epoch": 0}
    repo = users_repository

    async def get_by_id(session, user_id):
        return SimpleNamespace(
            id=USER, is_active=True, email_verified_at=datetime.now(UTC), sessions_valid_after=None, display_name="U"
        )

    async def access_state(session, organization_id):
        return (True, "active", 0)

    async def member_state(session, organization_id, user_id):
        return (state["deactivated"], state["epoch"])

    async def roles(session, u, o):
        return ["viewer"]

    async def perms(session, u, o):
        return ["incident:read"]

    async def project_perms(session, u, o):
        return {}

    async def no_context(session, organization_id):
        return None

    monkeypatch.setattr(repo, "get_by_id", get_by_id)
    monkeypatch.setattr(repo, "get_organization_access_state", access_state)
    monkeypatch.setattr(repo, "get_member_access_state", member_state)  # after the autouse stand-in
    monkeypatch.setattr(repo, "get_role_names", roles)
    monkeypatch.setattr(repo, "get_permission_codes", perms)
    monkeypatch.setattr(repo, "get_project_permission_map", project_perms)
    monkeypatch.setattr(users_service, "set_tenant_context", no_context)
    return state


@pytest.mark.asyncio
async def test_a_deactivated_member_is_refused_even_with_a_current_token(identity_fakes) -> None:
    identity_fakes.update(deactivated=True, epoch=1)
    with pytest.raises(PermissionDeniedError) as info:
        await users_service.resolve_identity(None, USER, ORG, token_member_epoch=1)
    assert info.value.error_code == "auth.membership_deactivated"
    with pytest.raises(PermissionDeniedError):  # ... and for callers that hold no token at all
        await users_service.resolve_identity(None, USER, ORG)


@pytest.mark.asyncio
async def test_a_token_from_an_older_member_epoch_stays_dead_after_reactivation(identity_fakes) -> None:
    identity_fakes.update(deactivated=False, epoch=2)  # deactivated once, reactivated once
    for stale in (0, 1, 3):
        with pytest.raises(PermissionDeniedError) as info:
            await users_service.resolve_identity(None, USER, ORG, token_member_epoch=stale)
        assert info.value.error_code == "auth.session_revoked"
    assert (await users_service.resolve_identity(None, USER, ORG, token_member_epoch=2)).organization_id == ORG


@pytest.mark.asyncio
async def test_a_member_never_deactivated_works_with_an_old_or_claimless_token(identity_fakes) -> None:
    assert (await users_service.resolve_identity(None, USER, ORG, token_member_epoch=0)).user_id == USER
    assert (await users_service.resolve_identity(None, USER, ORG)).user_id == USER


@pytest.mark.asyncio
async def test_a_suspended_organization_is_still_reported_first(identity_fakes, monkeypatch) -> None:
    async def suspended(session, organization_id):
        return (True, "suspended", 0)

    async def notice(session, organization_id):
        return {}

    monkeypatch.setattr(users_repository, "get_organization_access_state", suspended)
    monkeypatch.setattr(users_repository, "get_organization_suspension_notice", notice)
    identity_fakes.update(deactivated=True, epoch=1)
    with pytest.raises(PermissionDeniedError) as info:
        await users_service.resolve_identity(None, USER, ORG, token_member_epoch=0)
    assert info.value.error_code == "auth.organization_suspended"


# ---------------------------------------------------------------- service validation (no database)


def _actor(*permissions: str, user_id=None, org=ORG) -> Identity:
    uid = user_id or uuid.uuid4()
    return Identity(
        kind=ActorKind.USER, subject=str(uid), organization_id=org, user_id=uid,
        display_name="A", roles=("x",), permissions=frozenset(permissions),
    )


@pytest.mark.asyncio
async def test_only_tenancy_manage_in_the_own_organization_may_change_a_membership() -> None:
    target = uuid.uuid4()
    for actor in (_actor("incident:read"), _actor("tenancy:manage", org=uuid.uuid4())):
        with pytest.raises(PermissionDeniedError):
            await membership.deactivate_member(None, actor, ORG, target, reason="r")  # type: ignore[arg-type]
        with pytest.raises(PermissionDeniedError):
            await membership.reactivate_member(None, actor, ORG, target, reason="r")  # type: ignore[arg-type]
        with pytest.raises(PermissionDeniedError):
            await membership.get_member_status(None, actor, ORG, target)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_administrator_cannot_deactivate_themselves() -> None:
    me = uuid.uuid4()
    with pytest.raises(ConflictError) as info:
        await membership.deactivate_member(None, _actor("tenancy:manage", user_id=me), ORG, me, reason="r")  # type: ignore[arg-type]
    assert info.value.error_code == "member.cannot_deactivate_self"


@pytest.mark.asyncio
async def test_a_stranger_is_a_404(monkeypatch) -> None:
    async def not_member(session, user_id, organization_id):
        return False

    monkeypatch.setattr(users_repository, "is_organization_member", not_member)
    with pytest.raises(NotFoundError) as info:
        await membership.deactivate_member(None, _actor("tenancy:manage"), ORG, uuid.uuid4(), reason="r")  # type: ignore[arg-type]
    assert info.value.error_code == "member.not_found"


def test_a_reason_is_mandatory_trimmed_and_bounded() -> None:
    assert MemberStatusChange(reason="  left the company  ").reason == "left the company"
    for bad in ("", "   ", "x" * 1001):
        with pytest.raises(ValueError):
            MemberStatusChange(reason=bad)
