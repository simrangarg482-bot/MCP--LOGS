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

from app.core.auth import email_verification
from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest, SessionTokens
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.tenancy.schemas import InvitationAcceptRequest
from app.core.users.schemas import UserEmailState
from app.shared.security import hash_opaque_token


def _email_state(*, verified: bool = True, active: bool = True, has_password: bool = True):
    return UserEmailState(
        user_id=uuid.uuid4(),
        email="person@example.com",
        email_verified_at=datetime.now(timezone.utc) if verified else None,
        is_active=active,
        has_password=has_password,
    )


def _patch_user_state(monkeypatch, state) -> None:
    async def fake_state(session, user_id):
        return state

    monkeypatch.setattr(auth_service.users_service, "get_user_email_state_by_id", fake_state)


class _FakeRefreshTokenRow:
    def __init__(self, *, organization_id: uuid.UUID, revoked_at=None) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.organization_id = organization_id
        self.family_id = uuid.uuid4()
        self.revoked_at = revoked_at
        self.oauth_client_id = None
        self.expires_at = now + timedelta(days=30)


async def _organization_is_active(session, organization_id):
    """Stand-in for `_assert_organization_not_suspended`: the real one reads the database
    (covered against Postgres in `test_organization_suspension_postgres.py`)."""
    return 0  # the epoch


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

    async def fake_revoke_refresh_token(session, refresh_token_id, *, revoked_at) -> bool:
        return True  # the atomic claim succeeded

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
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token_if_active", fake_revoke_refresh_token)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)
    _patch_user_state(monkeypatch, _email_state())

    result = await auth_service.refresh(None, RefreshRequest(refresh_token="a-valid-token"))

    assert result.access_token == "new-access-token"
    assert call_order == [
        "resolve_org_id",
        "set_tenant_context",
        "get_refresh_token_by_hash",
        "issue_session",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_kwargs", "expected_code"),
    [
        ({"verified": False}, "auth.email_not_verified"),
        ({"active": False}, "user.inactive"),
        ({"verified": False, "active": False}, "user.inactive"),
    ],
)
async def test_refresh_is_refused_for_unverified_or_inactive_user_and_does_not_rotate(
    monkeypatch, state_kwargs, expected_code
) -> None:
    """A refresh token must not outlive the account state that made it
    acceptable: a legacy/unverified or deactivated account gets no new
    tokens, and the presented token is NOT rotated/revoked by the refusal."""
    organization_id = uuid.uuid4()
    token_row = _FakeRefreshTokenRow(organization_id=organization_id)
    rotated: list[object] = []
    issued: list[object] = []

    async def fake_resolve(session, token_hash):
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    async def fake_get_refresh_token_by_hash(session, token_hash):
        return token_row

    async def fake_revoke_refresh_token(session, refresh_token_id, *, revoked_at) -> bool:
        rotated.append(refresh_token_id)
        return True

    async def fake_issue_session(session, **kwargs):
        issued.append(kwargs)

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        auth_service.repository, "get_refresh_token_by_hash", fake_get_refresh_token_by_hash
    )
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token_if_active", fake_revoke_refresh_token)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)
    _patch_user_state(monkeypatch, _email_state(**state_kwargs))

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service.refresh(None, RefreshRequest(refresh_token="a-valid-token"))

    assert exc_info.value.error_code == expected_code
    assert rotated == [] and issued == []


@pytest.mark.asyncio
async def test_refresh_is_refused_when_the_user_row_is_gone(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    token_row = _FakeRefreshTokenRow(organization_id=organization_id)

    async def fake_resolve(session, token_hash):
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    async def fake_get_refresh_token_by_hash(session, token_hash):
        return token_row

    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        auth_service.repository, "get_refresh_token_by_hash", fake_get_refresh_token_by_hash
    )
    _patch_user_state(monkeypatch, None)

    with pytest.raises(PermissionDeniedError):
        await auth_service.refresh(None, RefreshRequest(refresh_token="a-valid-token"))


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


def _patch_invitation_org(monkeypatch, organization_id: uuid.UUID | None = None):
    """The accept endpoint is unauthenticated: the invitation's organization is
    resolved through the RLS-bypass function and tenant context is set BEFORE
    the RLS-protected `invitations` read. Returns the recorded context calls."""
    org = organization_id or uuid.uuid4()
    context_calls: list[uuid.UUID] = []

    async def fake_resolve(session, invitation_id):
        return org

    async def fake_set_tenant_context(session, org_id) -> None:
        context_calls.append(org_id)

    monkeypatch.setattr(auth_service.repository, "resolve_invitation_organization_id", fake_resolve)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    return context_calls


def _patch_no_existing_account(monkeypatch, *, exists: bool = False) -> None:
    async def fake_find(session, email):
        return uuid.uuid4() if exists else None

    monkeypatch.setattr(auth_service.users_service, "find_user_id_by_email_ci", fake_find)


@pytest.mark.asyncio
async def test_accept_invitation_with_password_only_starts_verification_and_creates_nothing(
    monkeypatch,
) -> None:
    """SECURITY: the invitation token is visible to the inviting admin, so it
    is not proof of mailbox control. A valid token must therefore only record a
    pending acceptance and hand back the mail to send -- no user, no password,
    no role, no invitation consumption, no session."""
    organization_id = uuid.uuid4()
    invitation = _FakeInvitationRow(organization_id=organization_id)
    context_calls = _patch_invitation_org(monkeypatch, organization_id)
    _patch_no_existing_account(monkeypatch)
    forbidden: list[str] = []
    started: dict[str, object] = {}

    async def fake_get_invitation_by_id(session, invitation_id):
        assert context_calls == [organization_id], "tenant context must be set before the RLS read"
        assert invitation_id == invitation.id
        return invitation

    async def fake_start(session, *, invitation_id, email, display_name, password_hash):
        started.update(
            invitation_id=invitation_id, email=email, display_name=display_name, password_hash=password_hash
        )
        return email_verification.PendingDelivery(uuid.uuid4(), "invitation", email, "raw-token")

    def _forbid(name):
        async def _inner(*args, **kwargs):
            forbidden.append(name)

        return _inner

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)
    monkeypatch.setattr(auth_service.email_verification, "start_invitation_acceptance", fake_start)
    monkeypatch.setattr(auth_service.users_service, "get_or_create_user", _forbid("get_or_create_user"))
    monkeypatch.setattr(auth_service.users_service, "set_password", _forbid("set_password"))
    monkeypatch.setattr(auth_service.users_service, "assign_role", _forbid("assign_role"))
    monkeypatch.setattr(auth_service.tenancy_service, "accept_invitation", _forbid("accept_invitation"))
    monkeypatch.setattr(auth_service, "_issue_session", _forbid("issue_session"))

    result = await auth_service.accept_invitation_with_password(
        None,
        invitation.id,
        InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
    )

    assert isinstance(result, email_verification.PendingDelivery)
    assert forbidden == []
    assert started["invitation_id"] == invitation.id
    assert started["email"] == invitation.email
    # only a bcrypt hash is carried forward, never the plaintext password
    assert started["password_hash"] != "a-strong-password"
    assert str(started["password_hash"]).startswith("$argon2id$")


@pytest.mark.asyncio
async def test_accept_invitation_with_password_refuses_to_adopt_an_existing_account(monkeypatch) -> None:
    """SECURITY regression (account takeover): an existing account with the
    invited email must NOT have its password overwritten or be issued a session
    by whoever holds the (admin-visible) invitation token."""
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4())
    _patch_invitation_org(monkeypatch, invitation.organization_id)
    _patch_no_existing_account(monkeypatch, exists=True)
    touched: list[str] = []

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    async def fake_start(*args, **kwargs):
        touched.append("start")

    async def fake_set_password(*args, **kwargs):
        touched.append("set_password")

    async def fake_issue_session(*args, **kwargs):
        touched.append("issue_session")

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)
    monkeypatch.setattr(auth_service.email_verification, "start_invitation_acceptance", fake_start)
    monkeypatch.setattr(auth_service.users_service, "set_password", fake_set_password)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)

    with pytest.raises(ConflictError) as exc_info:
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="attacker-chosen-password"),
        )

    assert exc_info.value.error_code == "invitation.account_exists"
    assert touched == []


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_invitation_with_no_resolvable_organization(
    monkeypatch,
) -> None:
    async def fake_resolve(session, invitation_id):
        return None

    monkeypatch.setattr(auth_service.repository, "resolve_invitation_organization_id", fake_resolve)

    with pytest.raises(NotFoundError):
        await auth_service.accept_invitation_with_password(
            None, uuid.uuid4(), InvitationAcceptRequest(token="whatever", password="a-strong-password")
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_unknown_invitation(monkeypatch) -> None:
    _patch_invitation_org(monkeypatch)
    _patch_no_existing_account(monkeypatch)
    async def fake_get_invitation_by_id(session, invitation_id):
        return None

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)

    with pytest.raises(NotFoundError):
        await auth_service.accept_invitation_with_password(
            None, uuid.uuid4(), InvitationAcceptRequest(token="whatever", password="a-strong-password")
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_wrong_token(monkeypatch) -> None:
    _patch_invitation_org(monkeypatch)
    _patch_no_existing_account(monkeypatch)
    """A caller who merely learned the invitation id (previously sufficient
    to accept it) must not be able to accept without also proving control of
    the invited email address via the real token.
    """
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), raw_token="the-real-token")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)

    with pytest.raises(PermissionDeniedError):
        await auth_service.accept_invitation_with_password(
            None, invitation.id, InvitationAcceptRequest(token="a-guessed-token", password="a-strong-password")
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_already_accepted_invitation(monkeypatch) -> None:
    _patch_invitation_org(monkeypatch)
    _patch_no_existing_account(monkeypatch)
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), status="accepted")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_expired_invitation(monkeypatch) -> None:
    _patch_invitation_org(monkeypatch)
    _patch_no_existing_account(monkeypatch)
    invitation = _FakeInvitationRow(
        organization_id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )


@pytest.mark.asyncio
async def test_accept_invitation_with_password_rejects_revoked_invitation(monkeypatch) -> None:
    _patch_invitation_org(monkeypatch)
    _patch_no_existing_account(monkeypatch)
    invitation = _FakeInvitationRow(organization_id=uuid.uuid4(), status="revoked")

    async def fake_get_invitation_by_id(session, invitation_id):
        return invitation

    monkeypatch.setattr(auth_service.tenancy_repository, "get_invitation_by_id", fake_get_invitation_by_id)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", _organization_is_active)

    with pytest.raises(ConflictError):
        await auth_service.accept_invitation_with_password(
            None,
            invitation.id,
            InvitationAcceptRequest(token="correct-raw-token", password="a-strong-password"),
        )
