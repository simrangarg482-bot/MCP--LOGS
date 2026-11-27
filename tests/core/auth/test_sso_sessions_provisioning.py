"""Focused tests for SSO provisioning delegation and EKIP session handling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest, VerifiedIdPClaims
from app.core.exceptions import PermissionDeniedError
from app.core.tenancy.schemas import ProvisioningDecision
from app.core.users.schemas import UserEmailState
from app.shared.security import KmsUnavailableError, hash_opaque_token

_JWT_SECRET = "unit-test-only-signing-key-with-32-bytes-minimum"


async def _noop_set_tenant_context(session, organization_id) -> None:
    """`_resolve_or_provision_user` now sets the tenant context explicitly (its
    first query hits an RLS-protected table). These orchestration tests run with
    `session=None`, so they stub it; the ordering itself is asserted in
    `tests/core/tenancy/test_create_organization_tenant_context.py`.
    """


def _email_state(
    *, verified: bool = True, active: bool = True, has_password: bool = True, email: str = "user@example.test"
) -> UserEmailState:
    return UserEmailState(
        user_id=uuid.uuid4(),
        email=email,
        email_verified_at=datetime.now(UTC) if verified else None,
        is_active=active,
        has_password=has_password,
    )


def _patch_user_state(monkeypatch, state) -> None:
    async def fake_state(session, user_id):
        return state

    monkeypatch.setattr(auth_service.users_service, "get_user_email_state_by_id", fake_state)


def _patch_membership(monkeypatch, member: bool = True) -> None:
    async def fake_holds(session, *, user_id, organization_id):
        return member

    monkeypatch.setattr(auth_service.users_service, "holds_any_role_in_organization", fake_holds)


def _settings():
    return SimpleNamespace(
        jwt_secret_key=_JWT_SECRET,
        jwt_algorithm="HS256",
        jwt_expiry_minutes=15,
        org_selection_token_expiry_minutes=10,
    )


@pytest.mark.asyncio
async def test_resolve_client_secret_rejects_invalid_or_missing_envelope() -> None:
    with pytest.raises(ValueError):
        await auth_service._resolve_client_secret("not-an-encrypted-envelope")
    with pytest.raises(ValueError):
        await auth_service._resolve_client_secret("")


@pytest.mark.asyncio
async def test_resolve_client_secret_does_not_hide_missing_kms_configuration(monkeypatch) -> None:
    def fail_to_get_kms():
        raise KmsUnavailableError("test KMS configuration unavailable")

    monkeypatch.setattr(auth_service, "get_kms", fail_to_get_kms)

    with pytest.raises(KmsUnavailableError, match="configuration unavailable"):
        await auth_service._resolve_client_secret("encrypted-envelope")


class _RefreshRow:
    def __init__(self, *, organization_id: uuid.UUID, revoked_at=None, expires_at=None) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.organization_id = organization_id
        self.family_id = uuid.uuid4()
        self.revoked_at = revoked_at
        self.oauth_client_id = None
        self.expires_at = expires_at or now + timedelta(days=30)


@pytest.mark.asyncio
async def test_issue_session_persists_only_refresh_token_hash(monkeypatch) -> None:
    user_id, organization_id, family_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tenant_context: list[uuid.UUID] = []
    inserted: dict[str, object] = {}

    async def fake_set_tenant_context(session, org_id):
        tenant_context.append(org_id)

    async def fake_insert_refresh_token(session, **kwargs):
        inserted.update(kwargs)

    monkeypatch.setattr(auth_service, "get_settings", _settings)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(auth_service.repository, "insert_refresh_token", fake_insert_refresh_token)

    async def organization_active(session, organization_id):
        return 0  # epoch; the status read needs a database; covered against Postgres

    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", organization_active)

    tokens = await auth_service._issue_session(
        None, user_id=user_id, organization_id=organization_id, family_id=family_id
    )
    claims = jose_jwt.decode(tokens.access_token, _JWT_SECRET, algorithms=["HS256"])

    assert tenant_context == [organization_id]
    assert claims["sub"] == str(user_id)
    assert claims["organization_id"] == str(organization_id)
    assert claims["type"] == "access"
    assert inserted["user_id"] == user_id
    assert inserted["organization_id"] == organization_id
    assert inserted["family_id"] == family_id
    assert inserted["token_hash"] == hash_opaque_token(tokens.refresh_token)
    assert inserted["token_hash"] != tokens.refresh_token


def test_verify_access_token_accepts_valid_signed_token(monkeypatch) -> None:
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    token, _, _ = auth_service._issue_access_token(user_id, organization_id)

    claims = auth_service.verify_access_token(token)

    assert claims.user_id == user_id
    assert claims.organization_id == organization_id


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": str(uuid.uuid4()), "organization_id": str(uuid.uuid4()), "type": "access"},
        {
            "sub": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "type": "org_selection",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
    ],
    ids=["expired-token", "wrong-token-purpose"],
)
def test_verify_access_token_rejects_expired_or_wrong_purpose_tokens(monkeypatch, claims) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    if claims.get("type") == "access":
        claims["iat"] = int((datetime.now(UTC) - timedelta(minutes=20)).timestamp())
        claims["exp"] = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
    token = jose_jwt.encode(claims, _JWT_SECRET, algorithm="HS256")

    with pytest.raises(PermissionDeniedError):
        auth_service.verify_access_token(token)


def test_verify_access_token_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    claims = {
        "sub": str(uuid.uuid4()),
        "organization_id": str(uuid.uuid4()),
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    token = jose_jwt.encode(claims, "another-signing-key-with-at-least-32-bytes", algorithm="HS256")

    with pytest.raises(PermissionDeniedError):
        auth_service.verify_access_token(token)


@pytest.mark.asyncio
async def test_refresh_rotates_token_in_same_family(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id)
    call_order: list[str] = []

    async def fake_resolve_org(session, token_hash):
        assert token_hash == hash_opaque_token("old-refresh-token")
        call_order.append("resolve_org")
        return organization_id

    async def fake_set_tenant(session, org_id):
        call_order.append("set_tenant")
        assert org_id == organization_id

    async def fake_get_row(session, token_hash):
        call_order.append("read_row")
        return row

    async def fake_revoke(session, row_id, *, revoked_at):
        call_order.append("revoke_old")
        assert row_id == row.id
        row.revoked_at = revoked_at
        return True  # the atomic claim succeeded

    async def fake_issue(session, *, user_id, organization_id, family_id):
        call_order.append("issue_new")
        assert user_id == row.user_id
        assert organization_id == row.organization_id
        assert family_id == row.family_id
        return "new-session"

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(
        auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org
    )
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token_if_active", fake_revoke)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue)
    _patch_user_state(monkeypatch, _email_state())

    result = await auth_service.refresh(None, RefreshRequest(refresh_token="old-refresh-token"))

    assert result == "new-session"
    assert row.revoked_at is not None
    assert call_order == ["resolve_org", "set_tenant", "read_row", "revoke_old", "issue_new"]


class _TxSession:
    """Records the transaction calls reuse handling must make, in order, into a shared event list."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def commit(self) -> None:
        self.events.append("commit")


def _wire_replay(monkeypatch, *, row, organization_id, events, claim_result=True, suspended=None, deactivated=None):
    """The minimal fakes `refresh()` needs, all appending to `events`."""

    async def fake_resolve_org(session, token_hash):
        return organization_id

    async def fake_set_tenant(session, org_id):
        assert org_id == organization_id
        events.append("set_tenant_context")

    async def fake_get_row(session, token_hash):
        return row

    async def fake_revoke_family(session, family_id, *, revoked_at):
        events.append(f"revoke_family:{family_id}")
        return 1

    async def fake_claim(session, row_id, *, revoked_at):
        events.append("claim")
        return claim_result

    async def organization_active(session, org_id):
        events.append("check_suspended")
        if suspended is not None:
            raise suspended
        return 0

    async def member_active(session, org_id, user_id):
        events.append("check_member")
        if deactivated is not None:
            raise deactivated
        return 0

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org)
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)
    monkeypatch.setattr(auth_service.repository, "revoke_family", fake_revoke_family)
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token_if_active", fake_claim)
    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", organization_active)
    monkeypatch.setattr(auth_service.membership, "assert_member_active", member_active)
    _patch_user_state(monkeypatch, _email_state())


_REUSE_SEQUENCE_TAIL = ["rollback", "set_tenant_context"]


@pytest.mark.asyncio
async def test_refresh_replay_revokes_token_family_durably_before_raising(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    events: list[str] = []
    _wire_replay(monkeypatch, row=row, organization_id=organization_id, events=events)

    with pytest.raises(PermissionDeniedError, match="session has been revoked") as info:
        await auth_service.refresh(_TxSession(events), RefreshRequest(refresh_token="replayed-token"))

    assert info.value.error_code == "auth.refresh_token_reused"
    # roll back FIRST (nothing unrelated may be committed), restore the tenant context, revoke the family, commit --
    # and only after the commit is the error raised (the `raises` block above ran after every recorded event).
    assert events[-4:] == ["rollback", "set_tenant_context", f"revoke_family:{row.family_id}", "commit"]
    assert events.index("check_suspended") < events.index("check_member") < events.index("rollback")  # real reasons first
    assert events.count("commit") == 1 and events.count("rollback") == 1
    assert "claim" not in events  # an already-revoked token never reaches the claim


@pytest.mark.asyncio
async def test_a_lost_atomic_claim_is_treated_as_reuse(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id)  # active when read ...
    events: list[str] = []
    _wire_replay(monkeypatch, row=row, organization_id=organization_id, events=events, claim_result=False)  # ... lost the race

    issued: list[object] = []

    async def must_not_issue(*args, **kwargs):
        issued.append(1)

    monkeypatch.setattr(auth_service, "_issue_session", must_not_issue)

    with pytest.raises(PermissionDeniedError) as info:
        await auth_service.refresh(_TxSession(events), RefreshRequest(refresh_token="raced-token"))

    assert info.value.error_code == "auth.refresh_token_reused"
    assert issued == []  # no descendant is minted for the loser
    assert events.index("claim") < events.index("rollback")
    assert events[-4:] == ["rollback", "set_tenant_context", f"revoke_family:{row.family_id}", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["suspended", "deactivated"])
async def test_suspension_and_deactivation_are_reported_as_such_and_revoke_nothing(monkeypatch, blocker) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    events: list[str] = []
    error = PermissionDeniedError("blocked", error_code=f"auth.{blocker}")
    _wire_replay(
        monkeypatch, row=row, organization_id=organization_id, events=events,
        suspended=error if blocker == "suspended" else None,
        deactivated=error if blocker == "deactivated" else None,
    )

    with pytest.raises(PermissionDeniedError) as info:
        await auth_service.refresh(_TxSession(events), RefreshRequest(refresh_token="revoked-by-policy"))

    assert info.value.error_code == f"auth.{blocker}"
    assert not any(e in ("rollback", "commit") or e.startswith("revoke_family") for e in events)


@pytest.mark.asyncio
async def test_a_wrong_oauth_client_does_not_trigger_family_revocation(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    row.oauth_client_id = "client-a"
    events: list[str] = []
    _wire_replay(monkeypatch, row=row, organization_id=organization_id, events=events)

    with pytest.raises(PermissionDeniedError) as info:
        await auth_service.refresh(
            _TxSession(events), RefreshRequest(refresh_token="t"), oauth_client_id="client-b"
        )

    assert info.value.error_code == "auth.invalid_refresh_token"
    assert not any(e in ("rollback", "commit") or e.startswith("revoke_family") for e in events)


@pytest.mark.asyncio
async def test_reuse_handling_discards_unrelated_pending_work_and_never_commits_it(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    events: list[str] = []
    _wire_replay(monkeypatch, row=row, organization_id=organization_id, events=events)

    class _Session(_TxSession):
        def __init__(self, events):
            super().__init__(events)
            self.pending = ["unrelated write"]
            self.committed: list[str] = []

        async def rollback(self) -> None:
            await super().rollback()
            self.pending.clear()  # a rollback discards pending work

        async def commit(self) -> None:
            await super().commit()
            self.committed.extend(self.pending)

    session = _Session(events)
    with pytest.raises(PermissionDeniedError):
        await auth_service.refresh(session, RefreshRequest(refresh_token="replayed-token"))

    assert session.committed == []  # the unrelated write was discarded before the security action committed


@pytest.mark.asyncio
async def test_a_failing_revocation_is_not_reported_as_reuse_and_leaves_the_session_clean(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    events: list[str] = []
    _wire_replay(monkeypatch, row=row, organization_id=organization_id, events=events)

    async def broken_revoke_family(session, family_id, *, revoked_at):
        raise RuntimeError("database went away")

    monkeypatch.setattr(auth_service.repository, "revoke_family", broken_revoke_family)

    with pytest.raises(RuntimeError, match="database went away"):  # NOT the reuse error: nothing was revoked
        await auth_service.refresh(_TxSession(events), RefreshRequest(refresh_token="replayed-token"))

    assert "commit" not in events
    assert events[-1] == "rollback"  # rolled back again, so the session is left in a safe state


@pytest.mark.asyncio
async def test_refresh_rejects_expired_and_unknown_tokens(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    expired_row = _RefreshRow(
        organization_id=organization_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    async def fake_resolve_org(session, token_hash):
        return organization_id if token_hash == hash_opaque_token("expired-token") else None

    async def fake_set_tenant(session, org_id):
        return None

    async def fake_get_row(session, token_hash):
        return expired_row

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(
        auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org
    )
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)

    with pytest.raises(PermissionDeniedError, match="expired"):
        await auth_service.refresh(None, RefreshRequest(refresh_token="expired-token"))
    with pytest.raises(PermissionDeniedError, match="Invalid refresh token"):
        await auth_service.refresh(None, RefreshRequest(refresh_token="unknown-token"))


@pytest.mark.asyncio
async def test_sso_provisioning_denial_is_owned_by_tenancy_even_if_email_exists(
    monkeypatch,
) -> None:
    """An existing account/email alone must not make SSO provisioning allowed."""
    monkeypatch.setattr(auth_service, "set_tenant_context", _noop_set_tenant_context)
    organization_id = uuid.uuid4()
    idp_claims = VerifiedIdPClaims(sub="subject", email="existing@example.test", email_verified=True)
    user_creation_calls: list[str] = []
    role_calls: list[dict[str, object]] = []

    async def fake_get_mapping(session, org_id, subject):
        return None

    async def fake_evaluate(session, *, organization_id, email, groups):
        assert email == "existing@example.test"
        assert groups == ()
        return ProvisioningDecision(allowed=False, reason="no_matching_policy")

    async def fake_resolve_user(*args, **kwargs):
        user_creation_calls.append("resolve_user_for_sso_provisioning")
        return uuid.uuid4()

    async def fake_assign_role(*args, **kwargs):
        role_calls.append(kwargs)

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", fake_evaluate)
    monkeypatch.setattr(
        auth_service.users_service, "resolve_user_for_sso_provisioning", fake_resolve_user
    )
    monkeypatch.setattr(auth_service.users_service, "assign_role", fake_assign_role)

    with pytest.raises(PermissionDeniedError, match="has not been provisioned"):
        await auth_service._resolve_or_provision_user(
            None,
            organization_id=organization_id,
            idp_subject=idp_claims.sub,
            idp_claims=idp_claims,
        )
    assert user_creation_calls == []
    assert role_calls == []


@pytest.mark.asyncio
async def test_sso_provisioning_assigns_role_from_tenancy_decision(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "set_tenant_context", _noop_set_tenant_context)
    organization_id, user_id, role_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invitation_id = uuid.uuid4()
    idp_claims = VerifiedIdPClaims(
        sub="subject", email="new@example.test", groups=("Engineering",), email_verified=True
    )
    calls: list[tuple[str, object]] = []

    async def fake_get_mapping(session, org_id, subject):
        return None

    async def fake_evaluate(session, *, organization_id, email, groups):
        assert groups == ("Engineering",)
        return ProvisioningDecision(
            allowed=True,
            grants_role_id=role_id,
            matched_invitation_id=invitation_id,
            reason="invitation_match",
        )

    async def fake_resolve_user(session, *, email, display_name):
        calls.append(("create", email))
        return user_id

    async def fake_assign_role(session, *, user_id, organization_id, role_id):
        calls.append(("role", role_id))

    async def fake_accept_invitation(session, passed_invitation_id):
        calls.append(("invitation", passed_invitation_id))

    async def fake_insert_mapping(session, *, organization_id, user_id, idp_subject):
        calls.append(("mapping", idp_subject))

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", fake_evaluate)
    monkeypatch.setattr(
        auth_service.users_service, "resolve_user_for_sso_provisioning", fake_resolve_user
    )
    monkeypatch.setattr(auth_service.users_service, "assign_role", fake_assign_role)
    monkeypatch.setattr(auth_service.tenancy_service, "accept_invitation", fake_accept_invitation)
    monkeypatch.setattr(
        auth_service.repository, "insert_external_identity_mapping", fake_insert_mapping
    )
    _patch_user_state(monkeypatch, _email_state())

    result = await auth_service._resolve_or_provision_user(
        None, organization_id=organization_id, idp_subject=idp_claims.sub, idp_claims=idp_claims
    )

    assert result == user_id
    assert calls == [
        ("create", "new@example.test"),
        ("role", role_id),
        ("invitation", invitation_id),
        ("mapping", "subject"),
    ]


@pytest.mark.asyncio
async def test_sso_provisioning_reuses_existing_external_identity_mapping(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "set_tenant_context", _noop_set_tenant_context)
    user_id = uuid.uuid4()

    async def fake_get_mapping(session, org_id, subject):
        return SimpleNamespace(user_id=user_id)

    async def should_not_evaluate(*args, **kwargs):
        raise AssertionError("mapped identities should not be provisioned again")

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", should_not_evaluate)
    _patch_user_state(monkeypatch, _email_state())
    _patch_membership(monkeypatch)

    result = await auth_service._resolve_or_provision_user(
        None,
        organization_id=uuid.uuid4(),
        idp_subject="existing-subject",
        idp_claims=VerifiedIdPClaims(
            sub="existing-subject", email="user@example.test", email_verified=True
        ),
    )

    assert result == user_id


# --- email-verification policy for SSO provisioning / linking ----------------------


@pytest.mark.asyncio
async def test_unverified_idp_claims_never_reach_invitation_matching_or_provisioning(monkeypatch) -> None:
    """Defence in depth: even if something built `VerifiedIdPClaims` without the
    evidence, nothing downstream (invitation match, domain rule, account link) runs."""
    monkeypatch.setattr(auth_service, "set_tenant_context", _noop_set_tenant_context)
    touched: list[str] = []

    async def spy(*args, **kwargs):
        touched.append("called")

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", spy)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", spy)
    monkeypatch.setattr(auth_service.users_service, "resolve_user_for_sso_provisioning", spy)
    unverified = VerifiedIdPClaims(sub="s", email="victim@corp.test")  # email_verified defaults to False

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service._resolve_or_provision_user(
            None, organization_id=uuid.uuid4(), idp_subject="s", idp_claims=unverified
        )

    assert exc_info.value.error_code == "auth.idp_email_not_verified"
    assert touched == []


def test_verified_idp_claims_default_to_unverified() -> None:
    assert VerifiedIdPClaims(sub="s", email="a@b.test").email_verified is False


@pytest.mark.asyncio
async def test_returning_sso_user_without_a_password_is_verified_by_the_idp_proof(monkeypatch) -> None:
    """A legacy SSO-only account (no local password, so no second credential a
    squatter could hold) whose own address the IdP has now proven is marked verified."""
    marked: list[tuple[uuid.UUID, str]] = []
    state = _email_state(verified=False, has_password=False, email="User@Example.test")
    _patch_user_state(monkeypatch, state)

    async def fake_mark(session, *, user_id, via):
        marked.append((user_id, via))

    monkeypatch.setattr(auth_service.users_service, "mark_email_verified", fake_mark)
    claims = VerifiedIdPClaims(sub="s", email="user@example.test", email_verified=True)

    await auth_service._ensure_mapped_user_email_verified(None, state.user_id, claims)

    assert marked == [(state.user_id, "oidc")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_kwargs",
    [
        {"has_password": True},  # a local password exists: a squatter could hold it
        {"has_password": False, "email": "different@example.test"},  # IdP proved another address
        {"has_password": False, "active": False},  # deactivated accounts are never revived
    ],
    ids=["has-password", "email-mismatch", "inactive"],
)
async def test_returning_sso_user_is_not_auto_verified_when_proof_does_not_cover_the_account(
    monkeypatch, state_kwargs
) -> None:
    marked: list[object] = []
    state = _email_state(verified=False, **state_kwargs)
    _patch_user_state(monkeypatch, state)

    async def fake_mark(*args, **kwargs):
        marked.append(args)

    monkeypatch.setattr(auth_service.users_service, "mark_email_verified", fake_mark)
    claims = VerifiedIdPClaims(sub="s", email="user@example.test", email_verified=True)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service._ensure_mapped_user_email_verified(None, state.user_id, claims)

    assert exc_info.value.error_code == "auth.email_not_verified"
    assert marked == []


@pytest.mark.asyncio
async def test_returning_sso_user_who_is_already_verified_is_untouched(monkeypatch) -> None:
    state = _email_state(verified=True)
    _patch_user_state(monkeypatch, state)

    async def fail(*args, **kwargs):
        raise AssertionError("must not re-verify")

    monkeypatch.setattr(auth_service.users_service, "mark_email_verified", fail)

    await auth_service._ensure_mapped_user_email_verified(
        None, state.user_id, VerifiedIdPClaims(sub="s", email="user@example.test", email_verified=True)
    )
