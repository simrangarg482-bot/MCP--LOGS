"""SSO configuration and login against REAL PostgreSQL, as the real `ekip_app` role.

What a mock cannot show: row-level security on `sso_configurations` and
`external_identity_mappings`, organization isolation of identity links, the
real provisioning / membership / inactive-user decisions, the real session
(refresh-token) issuance and rotation -- driven end to end by the SIMULATED
provider in `tests/support/mock_oidc.py` over real HTTP, with an in-memory
stand-in for Redis. Real identity providers are NOT involved.

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL,
migrated, disposable database (same contract as the other `*_postgres` tests).
Every row carries a per-test tag and is removed with targeted DELETEs.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from jose import jwt as jose_jwt
from sqlalchemy import text

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest, SSOCallbackRequest
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import AccessRuleCreate, SSOConfigurationCreate, SSOConfigurationUpdate
from app.shared.config.settings import get_settings
from tests.core.auth.test_sso_flow_mock_idp import FLAVORS, FRONTEND, MemoryStore
from tests.core.users.test_role_management_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _actor,
    _is_local,
    _make_org,
    _make_user,
    _setup_org,
    env,
)
from tests.support.mock_oidc import MockOidcProvider

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)


@pytest_asyncio.fixture
async def sso(env, monkeypatch):  # noqa: F811
    migrated = await env.sup(
        "SELECT 1 FROM information_schema.columns WHERE table_name='sso_configurations' AND column_name='enabled'"
    )
    if not migrated:
        pytest.skip("sso_configurations.enabled missing: run `alembic upgrade head` on this database")

    settings = get_settings().model_copy(
        update={"sso_allow_insecure_issuers": True, "cors_allowed_origins": [FRONTEND]}
    )
    monkeypatch.setattr(auth_service, "get_settings", lambda: settings)
    monkeypatch.setattr(tenancy_service, "get_settings", lambda: settings)
    auth_service._discovery_cache.clear()
    env.store = MemoryStore()
    env.idps = []

    def new_idp(flavor="okta") -> MockOidcProvider:
        idp = MockOidcProvider(flavor=flavor).start()
        env.idps.append(idp)
        return idp

    env.new_idp = new_idp
    yield env
    for idp in env.idps:
        idp.stop()
    auth_service._discovery_cache.clear()
    s = f"rm-{env.tag}-%"
    await env.sup(
        "DELETE FROM refresh_tokens WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)", s=s
    )


# ------------------------------------------------------------------ helpers


async def _configure(env, org_id, actor, idp, provider=None, **overrides):
    data = SSOConfigurationCreate(
        provider=provider or idp.flavor,
        issuer_url=idp.configured_issuer,
        client_id=idp.client_id,
        client_secret_ref=idp.client_secret,
        **overrides,
    )
    return await env.run(org_id, lambda s: tenancy_service.configure_sso(s, actor, org_id, data))


async def _sso_org(env, name, idp, *, provider=None):
    org_id, _role, admin_id = await _setup_org(env, name)
    actor = await _actor(env, org_id, admin_id)
    await _configure(env, org_id, actor, idp, provider)
    return org_id, actor


def _slug(env, name):
    return env.slug(name)


async def _rule(env, org_id, actor, rule_type="domain", value="corp.test", role="viewer"):
    data = AccessRuleCreate(rule_type=rule_type, value=value, grants_role=role)
    return await env.run(org_id, lambda s: tenancy_service.create_access_rule(s, actor, org_id, data))


async def _login(env, idp, slug, *, email, groups=None, sub=None):
    redirect = f"{FRONTEND}/o/{slug}/callback"
    begin = await env.run(
        None, lambda s: auth_service.begin_sso_login(s, slug, redirect_uri=redirect, store=env.store)
    )
    params = idp.browser_login(begin.authorization_url, email=email, groups=groups, sub=sub)
    data = SSOCallbackRequest(
        org_slug=slug, code=params["code"], state=params["state"], transaction_id=begin.transaction_id
    )
    return await env.run(
        None, lambda s: auth_service.complete_sso_login(s, data, redirect_uri=redirect, store=env.store)
    )


async def _expect(env, error_code, idp, slug, **kw):
    with pytest.raises((PermissionDeniedError, NotFoundError)) as info:
        await _login(env, idp, slug, **kw)
    assert info.value.error_code == error_code, (info.value.error_code, info.value.message)


async def _scalar(env, sql, **p):
    rows = await env.sup(sql, **p)
    return rows[0][0] if rows else None


# ============================================================ every flavor, real DB


@pytest.mark.asyncio
@pytest.mark.parametrize("flavor", FLAVORS)
async def test_full_sso_login_provisions_and_issues_a_real_session_for_each_provider(sso, flavor) -> None:
    idp = sso.new_idp(flavor)
    org_id, actor = await _sso_org(sso, f"p-{flavor[:4]}", idp)
    await _rule(sso, org_id, actor, "domain", "corp.test", "engineer")
    email = f"rm-{sso.tag}-{flavor[:4]}@corp.test"

    tokens = await _login(sso, idp, _slug(sso, f"p-{flavor[:4]}"), email=email)

    claims = jose_jwt.get_unverified_claims(tokens.access_token)
    user_id = await _scalar(sso, "SELECT id FROM users WHERE email=:e", e=email)
    assert claims["sub"] == str(user_id) and claims["organization_id"] == str(org_id)
    assert await _scalar(sso, "SELECT email_verified_at IS NOT NULL FROM users WHERE id=:u", u=user_id)
    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE user_id=:u", u=user_id) == 1
    assert await _scalar(
        sso,
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=:u AND ur.organization_id=:o",
        u=user_id,
        o=org_id,
    ) == "engineer"

    # the session behaves like any other: refresh rotates, reuse is detected, logout revokes
    rotated = await sso.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token)))
    assert rotated.refresh_token != tokens.refresh_token
    with pytest.raises(PermissionDeniedError) as reuse:
        await sso.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token)))
    assert reuse.value.error_code == "auth.refresh_token_reused"
    await sso.run(None, lambda s: auth_service.logout(s, RefreshRequest(refresh_token=rotated.refresh_token)))
    with pytest.raises(PermissionDeniedError):
        await sso.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=rotated.refresh_token)))


@pytest.mark.asyncio
async def test_session_expiry_is_enforced_for_sso_sessions(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "exp", idp)
    await _rule(sso, org_id, actor)
    tokens = await _login(sso, idp, _slug(sso, "exp"), email=f"rm-{sso.tag}-e@corp.test")
    await sso.sup("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute' WHERE organization_id=:o", o=org_id)

    with pytest.raises(PermissionDeniedError) as info:
        await sso.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token)))
    assert info.value.error_code == "auth.refresh_token_expired"


@pytest.mark.asyncio
async def test_group_rule_provisions_from_the_groups_claim_case_insensitively(sso) -> None:
    idp = sso.new_idp("okta")
    org_id, actor = await _sso_org(sso, "grp", idp)
    await _rule(sso, org_id, actor, "group", "engineering", "reviewer")

    await _login(sso, idp, _slug(sso, "grp"), email=f"rm-{sso.tag}-g@other.test", groups=["Engineering"])

    assert await _scalar(sso, "SELECT count(*) FROM users WHERE email=:e", e=f"rm-{sso.tag}-g@other.test") == 1
    await _expect(sso, "auth.not_provisioned", idp, _slug(sso, "grp"), email=f"rm-{sso.tag}-g2@other.test", groups=["sales"])


# ============================================================ who may sign in


@pytest.mark.asyncio
async def test_a_person_nobody_provisioned_is_refused_and_nothing_is_created(sso) -> None:
    idp = sso.new_idp()
    await _sso_org(sso, "np", idp)
    email = f"rm-{sso.tag}-np@corp.test"

    await _expect(sso, "auth.not_provisioned", idp, _slug(sso, "np"), email=email)

    assert await _scalar(sso, "SELECT count(*) FROM users WHERE email=:e", e=email) == 0


@pytest.mark.asyncio
async def test_returning_user_logs_in_again_through_the_identity_link_without_any_rule(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "ret", idp)
    rule = await _rule(sso, org_id, actor)
    email = f"rm-{sso.tag}-ret@corp.test"
    await _login(sso, idp, _slug(sso, "ret"), email=email)
    await sso.sup("DELETE FROM organization_access_rules WHERE id=:i", i=rule.id)

    tokens = await _login(sso, idp, _slug(sso, "ret"), email=email)  # same sub

    assert tokens.access_token


@pytest.mark.asyncio
async def test_inactive_user_cannot_sign_in_through_sso(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "ina", idp)
    await _rule(sso, org_id, actor)
    email = f"rm-{sso.tag}-ina@corp.test"
    await _login(sso, idp, _slug(sso, "ina"), email=email)
    await sso.sup("UPDATE users SET is_active=false WHERE email=:e", e=email)

    await _expect(sso, "user.inactive", idp, _slug(sso, "ina"), email=email)
    # a NEW identity landing on a deactivated account is refused too
    await _expect(sso, "user.inactive", idp, _slug(sso, "ina"), email=email, sub="another-subject")


@pytest.mark.asyncio
async def test_removed_member_with_a_stale_link_is_denied_and_the_link_grants_nothing(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "rem", idp)
    rule = await _rule(sso, org_id, actor)
    email = f"rm-{sso.tag}-rem@corp.test"
    await _login(sso, idp, _slug(sso, "rem"), email=email)
    user_id = await _scalar(sso, "SELECT id FROM users WHERE email=:e", e=email)
    # an administrator removes the person and the auto-join rule no longer applies
    await sso.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o", u=user_id, o=org_id)
    await sso.sup("DELETE FROM organization_access_rules WHERE id=:i", i=rule.id)

    for _ in range(2):  # repeatable: a denial never creates anything
        await _expect(sso, "auth.not_provisioned", idp, _slug(sso, "rem"), email=email)
    assert await _scalar(sso, "SELECT count(*) FROM user_roles WHERE user_id=:u", u=user_id) == 0
    assert await _scalar(sso, "SELECT count(*) FROM refresh_tokens WHERE user_id=:u AND created_at > now() - interval '1 minute'", u=user_id) == 1  # only the original login's session

    # if an administrator gives them a role again, the existing link works again
    role = await _scalar(sso, "SELECT id FROM roles WHERE name='viewer' AND organization_id=:o", o=org_id)
    await sso.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u, :o, :r)", u=user_id, o=org_id, r=role)
    assert (await _login(sso, idp, _slug(sso, "rem"), email=email)).access_token


@pytest.mark.asyncio
async def test_removed_member_can_return_only_if_current_rules_admit_them(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "back", idp)
    await _rule(sso, org_id, actor)
    email = f"rm-{sso.tag}-back@corp.test"
    await _login(sso, idp, _slug(sso, "back"), email=email)
    user_id = await _scalar(sso, "SELECT id FROM users WHERE email=:e", e=email)
    await sso.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o", u=user_id, o=org_id)

    tokens = await _login(sso, idp, _slug(sso, "back"), email=email)  # the domain rule still admits them

    assert tokens.access_token
    assert await _scalar(sso, "SELECT count(*) FROM user_roles WHERE user_id=:u AND organization_id=:o", u=user_id, o=org_id) == 1
    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE user_id=:u", u=user_id) == 1


@pytest.mark.asyncio
async def test_an_identity_link_in_one_organization_grants_nothing_in_another(sso) -> None:
    idp = sso.new_idp()
    org_a, actor_a = await _sso_org(sso, "xa", idp)
    org_b, _actor_b = await _sso_org(sso, "xb", idp)  # same provider, no rules
    await _rule(sso, org_a, actor_a)
    email = f"rm-{sso.tag}-x@corp.test"
    await _login(sso, idp, _slug(sso, "xa"), email=email)  # linked in A only

    await _expect(sso, "auth.not_provisioned", idp, _slug(sso, "xb"), email=email)

    user_id = await _scalar(sso, "SELECT id FROM users WHERE email=:e", e=email)
    assert await _scalar(sso, "SELECT count(*) FROM user_roles WHERE user_id=:u AND organization_id=:o", u=user_id, o=org_b) == 0
    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE organization_id=:o", o=org_b) == 0


@pytest.mark.asyncio
async def test_a_session_is_only_ever_for_the_organization_that_was_logged_into(sso) -> None:
    idp = sso.new_idp()
    org_a, actor_a = await _sso_org(sso, "sa", idp)
    org_b, actor_b = await _sso_org(sso, "sb", idp)
    await _rule(sso, org_a, actor_a)
    await _rule(sso, org_b, actor_b)
    email = f"rm-{sso.tag}-s@corp.test"

    ta = await _login(sso, idp, _slug(sso, "sa"), email=email)
    tb = await _login(sso, idp, _slug(sso, "sb"), email=email)

    assert jose_jwt.get_unverified_claims(ta.access_token)["organization_id"] == str(org_a)
    assert jose_jwt.get_unverified_claims(tb.access_token)["organization_id"] == str(org_b)


# ============================================================ disabled / suspended / unsupported


@pytest.mark.asyncio
async def test_every_unavailable_cause_gives_the_identical_error(sso) -> None:
    idp = sso.new_idp()
    org_off, actor_off = await _sso_org(sso, "off", idp)
    await sso.run(org_off, lambda s: tenancy_service.update_sso_config(s, actor_off, org_off, SSOConfigurationUpdate(enabled=False)))
    org_sus, _a = await _sso_org(sso, "sus", idp)
    await sso.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org_sus)
    org_saml, _b = await _sso_org(sso, "saml", idp)
    await sso.sup("UPDATE sso_configurations SET protocol='saml' WHERE organization_id=:o", o=org_saml)
    org_none, _role, _admin = await _setup_org(sso, "none")  # no SSO at all

    seen = set()
    for slug in (_slug(sso, "off"), _slug(sso, "sus"), _slug(sso, "saml"), _slug(sso, "none"), "no-such-org-at-all"):
        with pytest.raises(NotFoundError) as info:
            await sso.run(None, lambda s, slug=slug: tenancy_service.get_organization_sso_config(s, slug))
        seen.add((info.value.error_code, info.value.message, json.dumps(info.value.detail)))
        options = await sso.run(None, lambda s, slug=slug: auth_service.get_sso_options(s, slug))
        assert options.model_dump() == {"available": False, "provider": None, "provider_label": None}
        with pytest.raises(NotFoundError):
            await _login(sso, idp, slug, email=f"rm-{sso.tag}-u@corp.test")
    assert seen == {("sso.unavailable", "Single sign-on is not available for this organization.", "null")}


@pytest.mark.asyncio
async def test_disabling_sso_blocks_a_login_that_already_began(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "mid", idp)
    await _rule(sso, org_id, actor)
    slug = _slug(sso, "mid")
    redirect = f"{FRONTEND}/o/{slug}/callback"
    begin = await sso.run(None, lambda s: auth_service.begin_sso_login(s, slug, redirect_uri=redirect, store=sso.store))
    params = idp.browser_login(begin.authorization_url, email=f"rm-{sso.tag}-m@corp.test")

    await sso.run(org_id, lambda s: tenancy_service.update_sso_config(s, actor, org_id, SSOConfigurationUpdate(enabled=False)))

    data = SSOCallbackRequest(org_slug=slug, code=params["code"], state=params["state"], transaction_id=begin.transaction_id)
    with pytest.raises(NotFoundError) as info:
        await sso.run(None, lambda s: auth_service.complete_sso_login(s, data, redirect_uri=redirect, store=sso.store))
    assert info.value.error_code == "sso.unavailable"
    assert idp.token_requests == []  # the code was never exchanged
    assert await _scalar(sso, "SELECT count(*) FROM users WHERE email=:e", e=f"rm-{sso.tag}-m@corp.test") == 0


@pytest.mark.asyncio
async def test_public_options_expose_only_availability_and_provider(sso) -> None:
    idp = sso.new_idp("auth0")
    await _sso_org(sso, "opt", idp)

    options = await sso.run(None, lambda s: auth_service.get_sso_options(s, _slug(sso, "opt")))

    assert options.model_dump() == {"available": True, "provider": "auth0", "provider_label": "Auth0"}


# ============================================================ configuration management


@pytest.mark.asyncio
async def test_configure_normalizes_encrypts_and_rejects_unsupported_input(sso) -> None:
    org_id, _r, admin_id = await _setup_org(sso, "cfg")
    actor = await _actor(sso, org_id, admin_id)

    for bad, code in (
        (dict(protocol="saml", issuer_url="https://t.okta.com"), "sso.protocol_unsupported"),
        (dict(issuer_url="ftp://t.okta.com"), "sso.issuer_invalid"),
        (dict(issuer_url="https://t.okta.com?x=1"), "sso.issuer_invalid"),
        (dict(issuer_url="https://t.okta.com", client_secret_ref="  "), "sso.client_secret_required"),
        (dict(issuer_url="https://t.okta.com", client_id=" "), "sso.field_required"),
    ):
        # insecure issuers allowed for this fixture, so use ftp/query/etc. as the "bad" cases
        base = dict(provider="okta", issuer_url="https://t.okta.com", client_id="c", client_secret_ref="s")
        base.update(bad)
        with pytest.raises(ValidationError) as info:
            await sso.run(org_id, lambda s, base=base: tenancy_service.configure_sso(s, actor, org_id, SSOConfigurationCreate(**base)))
        assert info.value.error_code == code

    created = await sso.run(
        org_id,
        lambda s: tenancy_service.configure_sso(
            s,
            actor,
            org_id,
            SSOConfigurationCreate(provider="okta", issuer_url=" https://Acme.Okta.com/ ", client_id=" cid ", client_secret_ref="plain-secret"),
        ),
    )
    assert created.issuer_url == "https://acme.okta.com" and created.client_id == "cid" and created.enabled is True
    assert "plain-secret" not in created.model_dump_json()
    stored = await _scalar(sso, "SELECT client_secret_ref FROM sso_configurations WHERE organization_id=:o", o=org_id)
    assert stored and "plain-secret" not in stored  # envelope-encrypted at rest
    with pytest.raises(ConflictError):
        await _configure(sso, org_id, actor, sso.new_idp())


@pytest.mark.asyncio
async def test_update_disable_enable_and_rotate_secret_without_touching_identity_links(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "upd", idp)
    await _rule(sso, org_id, actor)
    await _login(sso, idp, _slug(sso, "upd"), email=f"rm-{sso.tag}-up@corp.test")
    before = await _scalar(sso, "SELECT client_secret_ref FROM sso_configurations WHERE organization_id=:o", o=org_id)

    off = await sso.run(org_id, lambda s: tenancy_service.update_sso_config(s, actor, org_id, SSOConfigurationUpdate(enabled=False)))
    assert off.enabled is False
    on = await sso.run(
        org_id,
        lambda s: tenancy_service.update_sso_config(
            s, actor, org_id, SSOConfigurationUpdate(enabled=True, client_secret_ref="new-secret", client_id="renamed")
        ),
    )
    assert on.enabled is True and on.client_id == "renamed" and "new-secret" not in on.model_dump_json()

    after = await _scalar(sso, "SELECT client_secret_ref FROM sso_configurations WHERE organization_id=:o", o=org_id)
    assert after != before and "new-secret" not in after
    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE organization_id=:o", o=org_id) == 1
    actions = await sso.sup("SELECT action, metadata FROM audit_logs WHERE organization_id=:o AND action LIKE 'sso_configuration.%'", o=org_id)
    assert {a[0] for a in actions} == {"sso_configuration.configure", "sso_configuration.update"}
    dumped = json.dumps([a[1] for a in actions], default=str)
    assert "new-secret" not in dumped and idp.client_secret not in dumped
    assert any(a[1].get("changed_fields") == ["client_id", "client_secret_ref", "enabled"] for a in actions if a[0].endswith("update"))
    # a no-op update records nothing
    count = len(actions)
    await sso.run(org_id, lambda s: tenancy_service.update_sso_config(s, actor, org_id, SSOConfigurationUpdate(enabled=True)))
    assert len(await sso.sup("SELECT 1 FROM audit_logs WHERE organization_id=:o AND action LIKE 'sso_configuration.%'", o=org_id)) == count


@pytest.mark.asyncio
async def test_changing_issuer_or_provider_needs_confirmation_and_then_clears_identity_links(sso) -> None:
    idp = sso.new_idp()
    org_id, actor = await _sso_org(sso, "chg", idp)
    await _rule(sso, org_id, actor)
    await _login(sso, idp, _slug(sso, "chg"), email=f"rm-{sso.tag}-c@corp.test")

    for change in (dict(issuer_url="https://other-idp.example.com"), dict(provider="entra_id")):
        with pytest.raises(ConflictError) as info:
            await sso.run(org_id, lambda s, change=change: tenancy_service.update_sso_config(s, actor, org_id, SSOConfigurationUpdate(**change)))
        assert info.value.error_code == "sso.identity_reset_required" and info.value.detail == {"identity_link_count": 1}
    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE organization_id=:o", o=org_id) == 1
    # the same issuer written differently is NOT a change
    same = await sso.run(
        org_id,
        lambda s: tenancy_service.update_sso_config(s, actor, org_id, SSOConfigurationUpdate(issuer_url=idp.configured_issuer.upper().replace("HTTP://", "http://") + "/")),
    )
    assert same.issuer_url == idp.configured_issuer.lower() or same.issuer_url.startswith("http://127.0.0.1")

    await sso.run(
        org_id,
        lambda s: tenancy_service.update_sso_config(
            s, actor, org_id, SSOConfigurationUpdate(issuer_url="https://other-idp.example.com", confirm_identity_reset=True)
        ),
    )

    assert await _scalar(sso, "SELECT count(*) FROM external_identity_mappings WHERE organization_id=:o", o=org_id) == 0
    audit = await sso.sup("SELECT metadata FROM audit_logs WHERE organization_id=:o AND action='sso_configuration.update' ORDER BY occurred_at DESC LIMIT 1", o=org_id)
    assert audit[0][0]["identity_links_cleared"] == 1


@pytest.mark.asyncio
async def test_only_administrators_of_the_same_organization_can_manage_sso(sso) -> None:
    idp = sso.new_idp()
    org_a, actor_a = await _sso_org(sso, "ma", idp)
    org_b, actor_b = await _sso_org(sso, "mb", idp)
    reader = actor_a.model_copy(update={"permissions": frozenset({"incident:read"})})

    # no tenancy:manage
    for call in (
        lambda s: tenancy_service.update_sso_config(s, reader, org_a, SSOConfigurationUpdate(enabled=False)),
        lambda s: tenancy_service.get_sso_config(s, reader, org_a),
    ):
        with pytest.raises(PermissionDeniedError):
            await sso.run(org_a, call)
    # an administrator of A naming B's id
    with pytest.raises(PermissionDeniedError):
        await sso.run(org_a, lambda s: tenancy_service.update_sso_config(s, actor_a, org_b, SSOConfigurationUpdate(enabled=False)))
    assert await _scalar(sso, "SELECT enabled FROM sso_configurations WHERE organization_id=:o", o=org_b) is True

    # row-level security as ekip_app: A's context cannot see or change B's row, even in raw SQL
    async def raw(s):
        seen = (await s.execute(text("SELECT count(*) FROM sso_configurations WHERE organization_id=:b"), {"b": org_b})).scalar()
        changed = (await s.execute(text("UPDATE sso_configurations SET enabled=false WHERE organization_id=:b"), {"b": org_b})).rowcount
        links = (await s.execute(text("SELECT count(*) FROM external_identity_mappings WHERE organization_id=:b"), {"b": org_b})).scalar()
        return seen, changed, links

    assert await sso.run(org_a, raw) == (0, 0, 0)
    assert await _scalar(sso, "SELECT enabled FROM sso_configurations WHERE organization_id=:o", o=org_b) is True
