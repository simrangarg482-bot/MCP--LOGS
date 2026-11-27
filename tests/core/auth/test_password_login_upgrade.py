"""Password login: Argon2id upgrade of legacy hashes, no lock-outs, no enumeration, suspension.

Real hashing (bcrypt and Argon2id) against a faked data layer -- the database behaviour is covered
by `test_password_reset_postgres.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import bcrypt
import pytest

from app.core.auth import passwords
from app.core.auth import service as auth_service
from app.core.auth.schemas import LoginRequest, OrganizationSelectionRequired, SessionTokens
from app.core.exceptions import PermissionDeniedError
from app.core.users.schemas import UserCredentialLookup

PASSWORD = "correct horse battery staple"


def _bcrypt() -> str:
    return bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


class Harness:
    def __init__(self, monkeypatch, *, stored_hash, active=True, verified=True, orgs=None):
        self.user_id = uuid.uuid4()
        self.upgrades: list[tuple[str, str]] = []
        self.issued: list[uuid.UUID] = []
        self.dummy_calls = 0
        self.orgs = orgs if orgs is not None else [SimpleNamespace(id=uuid.uuid4(), name="Acme", slug="acme", status="active")]
        lookup = (
            None
            if stored_hash == "NO_ACCOUNT"
            else UserCredentialLookup(
                user_id=self.user_id,
                password_hash=None if stored_hash == "NO_PASSWORD" else stored_hash,
                is_active=active,
                email_verified_at=datetime.now(UTC) if verified else None,
                email="person@corp.test",
            )
        )

        async def get_lookup(session, email):
            return lookup

        async def list_orgs(session, user_id):
            return [o.id for o in self.orgs]

        async def by_ids(session, ids):
            return list(self.orgs)

        async def upgrade(session, *, user_id, old_hash, new_hash):
            self.upgrades.append((old_hash, new_hash))
            return True

        async def issue(session, *, user_id, organization_id, family_id):
            self.issued.append(organization_id)
            return SessionTokens(access_token="a", refresh_token="r", expires_in=900)

        real_dummy = passwords.verify_against_dummy

        def dummy(password):
            self.dummy_calls += 1
            real_dummy(password)

        monkeypatch.setattr(auth_service.users_service, "get_credential_lookup", get_lookup)
        monkeypatch.setattr(auth_service.users_service, "list_organizations_for_login", list_orgs)
        monkeypatch.setattr(auth_service.users_service, "upgrade_password_hash", upgrade)
        monkeypatch.setattr(auth_service.tenancy_repository, "get_organizations_by_ids", by_ids)
        monkeypatch.setattr(auth_service, "_issue_session", issue)
        monkeypatch.setattr(auth_service.passwords, "verify_against_dummy", dummy)

    async def login(self, password=PASSWORD):
        return await auth_service.login_with_password(None, LoginRequest(email="person@corp.test", password=password))


@pytest.mark.asyncio
async def test_a_bcrypt_account_logs_in_and_is_upgraded_to_argon2id_after_success(monkeypatch) -> None:
    legacy = _bcrypt()
    h = Harness(monkeypatch, stored_hash=legacy)

    result = await h.login()

    assert isinstance(result, SessionTokens) and h.issued
    ((old, new),) = h.upgrades
    assert old == legacy and new.startswith("$argon2id$")
    assert passwords.verify_password(PASSWORD, new)  # the SAME password still works afterwards
    assert not passwords.needs_rehash(new)


@pytest.mark.asyncio
async def test_a_wrong_password_never_changes_the_stored_hash(monkeypatch) -> None:
    h = Harness(monkeypatch, stored_hash=_bcrypt())

    with pytest.raises(PermissionDeniedError) as info:
        await h.login("not the password at all")

    assert info.value.error_code == "auth.invalid_credentials"
    assert h.upgrades == [] and h.issued == []


@pytest.mark.asyncio
async def test_a_current_argon2id_hash_is_left_alone(monkeypatch) -> None:
    h = Harness(monkeypatch, stored_hash=passwords.hash_password(PASSWORD))
    await h.login()
    assert h.upgrades == [] and h.issued


@pytest.mark.asyncio
async def test_an_over_long_attempt_against_a_bcrypt_hash_is_a_plain_failure(monkeypatch) -> None:
    h = Harness(monkeypatch, stored_hash=bcrypt.hashpw(b"x" * 72, bcrypt.gensalt(rounds=4)).decode())
    with pytest.raises(PermissionDeniedError) as info:
        await h.login("x" * 72 + "extra")
    assert info.value.error_code == "auth.invalid_credentials" and h.upgrades == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"active": False}, {"verified": False}])
async def test_no_upgrade_and_no_session_for_inactive_or_unverified_accounts(monkeypatch, kwargs) -> None:
    h = Harness(monkeypatch, stored_hash=_bcrypt(), **kwargs)
    with pytest.raises(PermissionDeniedError):
        await h.login()
    assert h.upgrades == [] and h.issued == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["NO_ACCOUNT", "NO_PASSWORD"])
async def test_unknown_and_sso_only_accounts_fail_exactly_like_a_wrong_password(monkeypatch, missing) -> None:
    h = Harness(monkeypatch, stored_hash=missing)
    with pytest.raises(PermissionDeniedError) as info:
        await h.login()
    assert (info.value.error_code, info.value.message) == ("auth.invalid_credentials", "Invalid email or password.")
    assert h.dummy_calls == 1  # the same amount of hashing work was spent


@pytest.mark.asyncio
async def test_only_suspended_organizations_means_no_session_and_a_specific_error(monkeypatch) -> None:
    suspended = SimpleNamespace(id=uuid.uuid4(), name="Gone", slug="gone", status="suspended", status_message_public=None)
    h = Harness(monkeypatch, stored_hash=passwords.hash_password(PASSWORD), orgs=[suspended])
    with pytest.raises(PermissionDeniedError) as info:
        await h.login()
    assert info.value.error_code == "auth.organization_suspended" and h.issued == []


@pytest.mark.asyncio
async def test_a_suspended_organization_does_not_block_the_users_other_active_one(monkeypatch) -> None:
    active = SimpleNamespace(id=uuid.uuid4(), name="Live", slug="live", status="active")
    suspended = SimpleNamespace(id=uuid.uuid4(), name="Gone", slug="gone", status="suspended")
    h = Harness(monkeypatch, stored_hash=passwords.hash_password(PASSWORD), orgs=[suspended, active])

    result = await h.login()

    assert isinstance(result, SessionTokens) and h.issued == [active.id]  # straight in, no picker


@pytest.mark.asyncio
async def test_the_organization_picker_never_offers_a_suspended_one(monkeypatch) -> None:
    a = SimpleNamespace(id=uuid.uuid4(), name="A", slug="a", status="active")
    b = SimpleNamespace(id=uuid.uuid4(), name="B", slug="b", status="onboarding")  # a normal working state
    c = SimpleNamespace(id=uuid.uuid4(), name="C", slug="c", status="suspended")
    h = Harness(monkeypatch, stored_hash=passwords.hash_password(PASSWORD), orgs=[a, b, c])

    result = await h.login()

    assert isinstance(result, OrganizationSelectionRequired)
    assert {o.slug for o in result.organizations} == {"a", "b"}
