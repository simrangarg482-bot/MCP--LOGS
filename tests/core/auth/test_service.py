"""Tests for `app.core.auth.service.refresh`/`logout`'s Milestone 10 RLS
wiring -- not a full test suite for `core.auth.service` (no test
infrastructure for that module existed before this addition). Each starts
from a bare, client-presented refresh-token hash with no `Identity`/org
context yet, so both must resolve the owning organization via the
RLS-bypassing `resolve_refresh_token_organization_id` lookup and call
`set_tenant_context` before the real, RLS-scoped `get_refresh_token_by_hash`
query runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest, SessionTokens
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.tenancy.schemas import InvitationAcceptRequest
from app.shared.security import hash_opaque_token


class _FakeRefreshTokenRow:
    def __init__(self, *, organization_id: uuid.UUID, revoked_at=None) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.organization_id = organization_id
        self.family_id = uuid.uuid4()
        self.revoked_at = revoked_at
        self.expires_at = now + timedelta(days=30)


@pytest.mark.asyncio
async def test_refresh_raises_invalid_token_when_hash_unresolvable(monkeypatch) -> None:
    """No row matches this token hash at all -- the RLS-bypassing resolver
    itself returns None, so `refresh` must fail the same way it always has
    (invalid token), never reach `set_tenant_context` or the real query.
    """
    tenant_context_calls: list[uuid.UUID] = []

    async def fake_resolve(session, token_hash):
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        tenant_context_calls.append(org_id)

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)

    with pytest.raises(PermissionDeniedError):
        await auth_service.refresh(None, RefreshRequest(refresh_token="bogus-token"))

    assert tenant_context_calls == []


@pytest.mark.asyncio
async def test_refresh_sets_tenant_context_before_reading_full_token_row(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    token_row = _FakeRefreshTokenRow(organization_id=organization_id)
    call_order: list[str] = []

    async def fake_resolve(session, token_hash):
        call_order.append("resolve_org_id")
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_refresh_token_by_hash(session, token_hash):
        call_order.append("get_refresh_token_by_hash")
        return token_row

    async def fake_revoke_refresh_token(session, refresh_token_id, *, revoked_at) -> None:
        return None

    async def fake_issue_session(session, *, user_id, organization_id, family_id):
        call_order.append("issue_session")
        return SessionTokens(
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            token_type="bearer",
            expires_in=900,
        )

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        auth_service.repository, "get_refresh_token_by_hash", fake_get_refresh_token_by_hash
    )
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token", fake_revoke_refresh_token)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)

    result = await auth_service.refresh(None, RefreshRequest(refresh_token="a-valid-token"))

    assert result.access_token == "new-access-token"
    assert call_order == [
        "resolve_org_id",
        "set_tenant_context",
        "get_refresh_token_by_hash",
        "issue_session",
    ]


@pytest.mark.asyncio
async def test_logout_is_a_no_op_when_hash_unresolvable(monkeypatch) -> None:
    async def fake_resolve(session, token_hash):
        return None

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)

    # Should not raise -- logout is documented as idempotent even for a
    # token that resolves to nothing.
    await auth_service.logout(None, RefreshRequest(refresh_token="bogus-token"))


@pytest.mark.asyncio
async def test_logout_sets_tenant_context_before_revoking(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    token_row = _FakeRefreshTokenRow(organization_id=organization_id)
    call_order: list[str] = []

    async def fake_resolve(session, token_hash):
        call_order.append("resolve_org_id")
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_refresh_token_by_hash(session, token_hash):
        call_order.append("get_refresh_token_by_hash")
        return token_row

    async def fake_revoke_refresh_token(session, refresh_token_id, *, revoked_at) -> None:
        call_order.append("revoke_refresh_token")
        assert refresh_token_id == token_row.id

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        auth_service.repository, "get_refresh_token_by_hash", fake_get_refresh_token_by_hash
    )
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token", fake_revoke_refresh_token)

    await auth_service.logout(None, RefreshRequest(refresh_token="a-valid-token"))

    assert call_order == [
        "resolve_org_id",
        "set_tenant_context",
        "get_refresh_token_by_hash",
        "revoke_refresh_token",
    ]


@pytest.mark.asyncio
async def test_revoke_all_sessions_scopes_by_user_and_organization(monkeypatch) -> None:
    """`revoke_all_sessions` ("logout everywhere") must delegate to the
    org-scoped repository call with the exact `user_id`/`organization_id`
    it was given, and return the revoked-session count as-is -- this is
    what both `POST /auth/logout-all` (self) and
    `POST /users/{user_id}/logout-all` (admin, on someone else's behalf)
    rely on.
    """
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_revoke_all_for_user(session, passed_user_id, passed_organization_id, *, revoked_at):
        captured["user_id"] = passed_user_id
        captured["organization_id"] = passed_organization_id
        captured["revoked_at"] = revoked_at
        return 4

    monkeypatch.setattr(auth_service.repository, "revoke_all_for_user", fake_revoke_all_for_user)

    result = await auth_service.revoke_all_sessions(None, user_id, organization_id)

    assert result == 4
    assert captured["user_id"] == user_id
    assert captured["organization_id"] == organization_id
    assert captured["revoked_at"] is not None


@pytest.mark.asyncio
async def test_resolve_client_secret_decrypts_an_envelope_encrypted_reference() -> None:
    """Regression test for the SSO client-secret KMS bypass: `_resolve_
    client_secret` must decrypt a real envelope produced by `core.tenancy.
    service.configure_sso`'s encrypt-at-write half of this same split, not
    treat the stored reference as if it were already plaintext.
    """
    from app.shared.security import encrypt_secret, get_kms

    plaintext_secret = "super-secret-oidc-client-secret"
    encrypted_client_secret_ref = await encrypt_secret(get_kms(), plaintext_secret)

    assert encrypted_client_secret_ref != plaintext_secret
    assert await auth_service._resolve_client_secret(encrypted_client_secret_ref) == plaintext_secret


class _FakeInvitationRow:
    def __init__(
        self,
        *,
        organization_id: uuid.UUID,
        email: str = "new.hire@acme.com",
        raw_token: str = "correct-raw-token",
        status: str = "pending",
        expires_at: datetime | None = None,
        grants_role_id: uuid.UUID | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.email = email
        self.token_hash = hash_opaque_token(raw_token)
        self.status = status
        self.expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(days=1))
        self.grants_role_id = grants_role_id or uuid.uuid4()


@pytest.mark.asyncio
async def test_accept_invitation_with_password_provisions_user_and_issues_session(monkeypatch) -> None:
    """The happy path: a valid, matching token provisions a real user
    (`get_or_create_user` -> `set_password` -> `assign_role`), consumes the
    invitation via the unchanged `tenancy_service.accept_invitation`, and
    returns a real session -- the exact gap Phase 7.5 closes (previously
    `accept_invitation` alone did none of this).
    """
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    invitation = _FakeInvitationRow(organization_id=organization_id)
    call_order: list[str] = []

    async def fake_get_invitation_by_id(session, invitation_id):
        assert invitation_id == invitation.id
        return invitation

    async def fake_get_or_create_user(session, *, email, display_name):
        call_order.append("get_or_create_user")
        assert email == invitation.email
        return user_id

    async def fake_set_password(session, *, user_id: uuid.UUID, password_hash: str) -> None:
        call_order.append("set_password")

    async def fake_assign_role(session, *, user_id, organization_id, role_id) -> None:
        call_order.append("assign_role")
        assert role_id == invitation.grants_role_id
        assert organization_id == invitation.organization_id

    async def fake_accept_invitation(session, invitation_id) -> None:
        call_order.append("accept_invitation")
        assert invitation_id == invitation.id

    async def fake_issue_session(session, *, user_id, organization_id, family_id):
        call_order.append("issue_session")
        return SessionTokens(access_token="access-token", refresh_token="refresh-token", expires_in=900)

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service.users_service, "get_or_create_user", fake_get_or_create_user)
    monkeypatch.setattr(auth_service.users_service, "set_password", fake_set_password)
    monkeypatch.setattr(auth_service.users_service, "assign_role", fake_assign_role)
    monkeypatch.setattr(auth_service.tenancy_service, "accept_invitation", fake_accept_invitation)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)

    result = await auth_service.accept_invitation_with_password(
        None,
        invitation.id,
        InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
    )

    assert result.access_token == "access-token"
    assert call_order == ["get_or_create_user", "set_password", "assign_role", "accept_invitation", "issue_session"]


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_unknown_invitation(monkeypatch) -> None:
    async def fake_get_invitation_by_id(session, invitation_id):
        return None

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)

    with pytest.raises(NotFoundError):
        await auth_service.accept_invitation_with_password(
            None, uuid.uuid4(), InvitationAcceptRequest(token="whatever", password="a-strong-password")
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_wrong_token(monkeypatch) -> None:
    """A caller who merely learned the invitation id (previously sufficient
    to accept it) must not be able to accept without also proving control of
    the invited email address via the real token.
    """
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), raw_token="the-real-token")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)

    with pytest.raises(PermissionDeniedError):
        await auth_service.accept_invitation_with_password(
            None, invitation.id, InvitationAcceptRequest(token="a-guessed-token", password="a-strong-password")
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_already_accepted_invitation(monkeypatch) -> None:
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), status="accepted")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_expired_invitation(monkeypatch) -> None:
    invitation = _FakeInvitationRow(
        organization_id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_revoked_invitation(monkeypatch) -> None:
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), status="revoked")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )
