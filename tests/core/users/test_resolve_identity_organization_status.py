"""The central check in `resolve_identity` (fakes for the data layer; the real-database behaviour is in
`tests/core/auth/test_organization_suspension_postgres.py`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.users import service as users_service

USER, ORG = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def fakes(monkeypatch):
    state = {"org": (True, "active"), "calls": []}
    repo = users_service.repository

    async def get_by_id(session, user_id):
        state["calls"].append("user")
        return SimpleNamespace(
            id=USER, is_active=True, email_verified_at=datetime.now(UTC), sessions_valid_after=None, display_name="U"
        )

    async def get_organization_access_state(session, organization_id):
        state["calls"].append(("org", organization_id))
        return (*state["org"], state.get("epoch", 0))

    async def roles(session, u, o):
        state["calls"].append("roles")
        return ["viewer"]

    async def perms(session, u, o):
        return ["incident:read"]

    async def project_perms(session, u, o):
        return {}

    async def no_context(session, organization_id):
        return None

    monkeypatch.setattr(repo, "get_by_id", get_by_id)
    monkeypatch.setattr(repo, "get_organization_access_state", get_organization_access_state)
    async def notice(session, organization_id):
        return {"organization_name": "Acme", "customer_message": state.get("message")}

    monkeypatch.setattr(repo, "get_organization_suspension_notice", notice)
    monkeypatch.setattr(repo, "get_role_names", roles)
    monkeypatch.setattr(repo, "get_permission_codes", perms)
    monkeypatch.setattr(repo, "get_project_permission_map", project_perms)
    monkeypatch.setattr(users_service, "set_tenant_context", no_context)
    return state


async def _resolve():
    return await users_service.resolve_identity(None, USER, ORG)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["onboarding", "active", "Active", " onboarding "])
async def test_onboarding_and_active_organizations_resolve_normally(fakes, status) -> None:
    fakes["org"] = (True, status)
    identity = await _resolve()
    assert identity.organization_id == ORG and identity.roles == ("viewer",)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["suspended", "Suspended", "  SUSPENDED "])
async def test_a_suspended_organization_is_refused_before_roles_are_even_read(fakes, status) -> None:
    fakes["org"] = (True, status)
    with pytest.raises(PermissionDeniedError) as info:
        await _resolve()
    assert info.value.error_code == "auth.organization_suspended"
    assert "roles" not in fakes["calls"]  # nothing about the tenant is loaded for a refused caller


@pytest.mark.asyncio
async def test_the_check_is_for_the_token_s_own_organization_only(fakes) -> None:
    other = uuid.uuid4()

    async def by_org(session, organization_id):
        return (True, "suspended" if organization_id == ORG else "active", 0)

    users_service.repository.get_organization_access_state = by_org  # type: ignore[assignment]
    with pytest.raises(PermissionDeniedError):
        await users_service.resolve_identity(None, USER, ORG)
    assert (await users_service.resolve_identity(None, USER, other)).organization_id == other


@pytest.mark.asyncio
async def test_a_missing_organization_is_not_found_not_a_pass(fakes) -> None:
    fakes["org"] = (False, None)
    with pytest.raises(NotFoundError) as info:
        await _resolve()
    assert info.value.error_code == "organization.not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["trial", "", None])
async def test_an_unrecognized_status_is_allowed_but_logged(fakes, status, monkeypatch) -> None:
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(users_service.logger, "warning", lambda event, **kw: warnings.append((event, kw)))
    fakes["org"] = (True, status)

    assert (await _resolve()).organization_id == ORG
    assert warnings and warnings[0][0] == "organization_status_unrecognized"
    assert warnings[0][1] == {"organization_id": str(ORG)}  # the id only


@pytest.mark.asyncio
async def test_the_session_cutoff_still_applies_and_is_checked_first(fakes, monkeypatch) -> None:
    async def revoked_user(session, user_id):
        return SimpleNamespace(
            id=USER, is_active=True, email_verified_at=datetime.now(UTC),
            sessions_valid_after=datetime(2030, 1, 1, tzinfo=UTC), display_name="U",
        )

    monkeypatch.setattr(users_service.repository, "get_by_id", revoked_user)
    with pytest.raises(PermissionDeniedError) as info:
        await users_service.resolve_identity(None, USER, ORG, token_issued_at=datetime(2029, 1, 1, tzinfo=UTC))
    assert info.value.error_code == "auth.session_revoked"


# --- Phase 3: the organization session epoch ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_from_an_older_epoch_is_refused_even_though_the_organization_is_active(fakes) -> None:
    fakes["epoch"] = 2  # suspended once and reactivated once
    for stale in (0, 1, 3):
        with pytest.raises(PermissionDeniedError) as info:
            await users_service.resolve_identity(None, USER, ORG, token_org_epoch=stale)
        assert info.value.error_code == "auth.session_revoked"
    assert (await users_service.resolve_identity(None, USER, ORG, token_org_epoch=2)).organization_id == ORG


@pytest.mark.asyncio
async def test_a_token_without_the_claim_is_epoch_zero_and_a_caller_that_passes_nothing_is_not_checked(fakes) -> None:
    assert (await users_service.resolve_identity(None, USER, ORG, token_org_epoch=0)).organization_id == ORG
    fakes["epoch"] = 1
    with pytest.raises(PermissionDeniedError):
        await users_service.resolve_identity(None, USER, ORG, token_org_epoch=0)
    # callers that hold no token at all (None) are not subject to the epoch rule
    assert (await users_service.resolve_identity(None, USER, ORG)).organization_id == ORG


@pytest.mark.asyncio
async def test_a_suspended_organization_reports_suspended_before_the_epoch_mismatch(fakes) -> None:
    fakes["org"] = (True, "suspended")
    fakes["epoch"] = 1
    with pytest.raises(PermissionDeniedError) as info:
        await users_service.resolve_identity(None, USER, ORG, token_org_epoch=0)
    assert info.value.error_code == "auth.organization_suspended"


@pytest.mark.asyncio
async def test_a_refusal_carries_only_the_customer_facing_message(fakes) -> None:
    fakes["org"] = (True, "suspended")
    fakes["message"] = "Contact billing"
    with pytest.raises(PermissionDeniedError) as info:
        await users_service.resolve_identity(None, USER, ORG)
    assert info.value.error_code == "auth.organization_suspended"
    assert info.value.detail == {"organization_name": "Acme", "customer_message": "Contact billing"}
