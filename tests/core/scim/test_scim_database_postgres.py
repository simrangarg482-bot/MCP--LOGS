"""SCIM database rules against REAL PostgreSQL as `ekip_app`: row-level security on every SCIM table, the narrow
token-lookup function, constraints, and the SSO proof-link. Skipped unless EKIP_TEST_* point at a LOCAL
migrated database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth import service as auth_service
from app.core.auth.schemas import VerifiedIdPClaims
from app.core.scim import tokens
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _org,
    _user,
    env,
    pw,
)

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

TABLES = ("scim_configurations", "scim_tokens", "scim_identities", "scim_pending_actions")


@pytest_asyncio.fixture(autouse=True)
async def _migrated(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='scim_identities'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


async def _seed(pw, org, tag):  # noqa: F811
    await pw.sup("INSERT INTO scim_configurations (organization_id, enabled) VALUES (:o, true) ON CONFLICT DO NOTHING", o=org)
    raw = tokens.generate_raw_token()
    await pw.sup(
        "INSERT INTO scim_tokens (organization_id, name, token_hash, token_prefix, expires_at) VALUES (:o,:n,:h,:p,now()+interval '1 day')",
        o=org, n=tag, h=tokens.hash_token(raw), p=raw[:12],
    )
    ident = (await pw.sup(
        "INSERT INTO scim_identities (organization_id, user_name, user_name_key) VALUES (:o,:n,:k) RETURNING id",
        o=org, n=f"{tag}@example.test", k=f"{tag}@example.test",
    ))[0][0]
    member = await _user(pw, f"seed{tag}{uuid.uuid4().hex[:4]}", org)
    await pw.sup(
        "INSERT INTO scim_pending_actions (organization_id, scim_identity_id, user_id, reason) VALUES (:o,:i,:u,'held')",
        o=org, i=ident, u=member,
    )
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TABLES)
async def test_every_scim_table_is_force_rls_and_confines_the_application_role(pw, table) -> None:  # noqa: F811
    flags = (await pw.sup("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:t", t=table))[0]
    assert flags == (True, True)
    assert [r[0] for r in await pw.sup("SELECT policyname FROM pg_policies WHERE tablename=:t", t=table)] == ["tenant_isolation"]

    short = table.replace("scim_", "").replace("_", "")[:6]
    a, b = await _org(pw, f"{short}a"), await _org(pw, f"{short}b")
    await _seed(pw, a, "a")
    await _seed(pw, b, "b")

    async def visible(s):
        return {r[0] for r in (await s.execute(text(f"SELECT organization_id FROM {table}"))).all()}

    assert await pw.run(a, visible) == {a}  # no WHERE clause at all, still only its own rows
    assert await pw.run(b, visible) == {b}

    async def cross_update(s):  # tenant context = A, the statement names B
        res = await s.execute(text(f"UPDATE {table} SET organization_id = organization_id WHERE organization_id=:b"), {"b": b})
        return res.rowcount

    assert await pw.run(a, cross_update) == 0


@pytest.mark.asyncio
async def test_the_application_role_cannot_write_another_organizations_scim_rows(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "wa"), await _org(pw, "wb")

    async def insert_into_b(s):
        await s.execute(
            text("INSERT INTO scim_identities (organization_id, user_name, user_name_key) VALUES (:b,'x@example.test','x@example.test')"),
            {"b": b},
        )

    with pytest.raises(DBAPIError) as info:
        await pw.run(a, insert_into_b)
    assert "row-level security" in str(info.value).lower()

    async def token_into_b(s):
        await s.execute(
            text("INSERT INTO scim_tokens (organization_id, name, token_hash, token_prefix, expires_at) VALUES (:b,'t','h','p',now())"),
            {"b": b},
        )

    with pytest.raises(DBAPIError):
        await pw.run(a, token_into_b)


@pytest.mark.asyncio
async def test_token_authentication_uses_a_narrow_security_definer_function(pw) -> None:  # noqa: F811
    row = (
        await pw.sup(
            "SELECT p.prosecdef, p.proconfig::text, has_function_privilege('ekip_app', p.oid, 'EXECUTE'), "
            "has_function_privilege('public', p.oid, 'EXECUTE') FROM pg_proc p WHERE p.proname='scim_authenticate_token'"
        )
    )[0]
    assert row[0] is True and "search_path=pg_catalog, pg_temp" in row[1] and row[2] is True and row[3] is False

    org = await _org(pw, "fn")
    raw = await _seed(pw, org, "fn")
    other = await _org(pw, "fnother")
    other_raw = await _seed(pw, other, "other")

    async def lookup(s, value):
        return (await s.execute(text("SELECT * FROM scim_authenticate_token(:h)"), {"h": tokens.hash_token(value)})).first()

    # works with NO tenant context (the token arrives before one exists) and returns only ids + expiry + revocation
    found = await pw.run(None, lambda s: lookup(s, raw))
    assert found is not None and found[1] == org and len(found) == 4
    assert (await pw.run(None, lambda s: lookup(s, other_raw)))[1] == other  # each token resolves to its OWN organization
    assert await pw.run(None, lambda s: lookup(s, "scim_unknown")) is None
    # ... and the application role still cannot read the table directly without a context
    async def direct(s):
        return (await s.execute(text("SELECT count(*) FROM scim_tokens"))).scalar_one()

    try:
        assert await pw.run(None, direct) == 0
    except DBAPIError:
        pass


@pytest.mark.asyncio
async def test_constraints_protect_the_model(pw) -> None:  # noqa: F811
    org = await _org(pw, "cons")
    await _seed(pw, org, "x")
    for sql in (
        "INSERT INTO scim_identities (organization_id, user_name, user_name_key) VALUES (:o,'X@example.test','x@example.test')",  # duplicate key
        "INSERT INTO scim_identities (organization_id, user_name, user_name_key, linked_via) VALUES (:o,'y','y','magic')",  # bad link source
        "INSERT INTO scim_configurations (organization_id, guard_percent) VALUES (:o, 0)",
        "UPDATE scim_configurations SET guard_window_minutes = 5000 WHERE organization_id=:o",
        "UPDATE scim_configurations SET guard_min_absolute = 0 WHERE organization_id=:o",
        "INSERT INTO scim_tokens (organization_id, name, token_hash, token_prefix, expires_at) VALUES (:o,'dup',(SELECT token_hash FROM scim_tokens LIMIT 1),'p',now())",
    ):
        with pytest.raises(DBAPIError):
            await pw.sup(sql, o=org)
    # a deleted identity frees its userName; two identities cannot claim the same member or externalId
    await pw.sup("UPDATE scim_identities SET deleted_at=now() WHERE organization_id=:o", o=org)
    await pw.sup("INSERT INTO scim_identities (organization_id, user_name, user_name_key) VALUES (:o,'x@example.test','x@example.test')", o=org)
    member = await _user(pw, "consmember", org)
    await pw.sup("INSERT INTO scim_identities (organization_id, user_name, user_name_key, user_id, linked_via) VALUES (:o,'m1','m1',:u,'admin')", o=org, u=member)
    with pytest.raises(DBAPIError):
        await pw.sup("INSERT INTO scim_identities (organization_id, user_name, user_name_key, user_id, linked_via) VALUES (:o,'m2','m2',:u,'admin')", o=org, u=member)


@pytest.mark.asyncio
async def test_a_successful_sso_login_links_a_scim_identity_whose_externalid_is_the_verified_subject(pw) -> None:  # noqa: F811
    org = await _org(pw, "sso")
    member = await _user(pw, "ssomember", org, password=None)
    subject = f"idp-{uuid.uuid4().hex[:8]}"
    await pw.sup("INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject) VALUES (:o,:u,:s)", o=org, u=member, s=subject)
    await pw.sup("INSERT INTO scim_identities (organization_id, user_name, user_name_key, external_id) VALUES (:o,:n,:n,:s)", o=org, n=pw.email("ssomember"), s=subject)
    decoy = await pw.sup(
        "INSERT INTO scim_identities (organization_id, user_name, user_name_key, external_id) VALUES (:o,'decoy@example.test','decoy@example.test','another-subject') RETURNING id", o=org
    )
    claims = VerifiedIdPClaims(sub=subject, email=pw.email("ssomember"), email_verified=True, name="S", groups=())
    resolved = await pw.run(org, lambda s: auth_service._resolve_or_provision_user(s, organization_id=org, idp_subject=subject, idp_claims=claims))
    assert resolved == member
    linked = await pw.sup("SELECT user_id, linked_via FROM scim_identities WHERE organization_id=:o AND external_id=:s", o=org, s=subject)
    assert linked == [(member, "sso_subject")]
    assert (await pw.sup("SELECT user_id FROM scim_identities WHERE id=:i", i=decoy[0][0]))[0][0] is None  # nothing else was linked
    # a second login changes nothing
    assert await pw.run(org, lambda s: auth_service._resolve_or_provision_user(s, organization_id=org, idp_subject=subject, idp_claims=claims)) == member


@pytest.mark.asyncio
async def test_a_failing_link_never_breaks_the_login(pw, monkeypatch) -> None:  # noqa: F811
    from app.core.scim import service as scim_service

    org = await _org(pw, "ssofail")
    member = await _user(pw, "ssofailmember", org, password=None)
    subject = f"idp-{uuid.uuid4().hex[:8]}"
    await pw.sup("INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject) VALUES (:o,:u,:s)", o=org, u=member, s=subject)
    await pw.sup("INSERT INTO scim_identities (organization_id, user_name, user_name_key, external_id) VALUES (:o,'f@example.test','f@example.test',:s)", o=org, s=subject)

    async def explode(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(scim_service, "link_identity", explode)
    claims = VerifiedIdPClaims(sub=subject, email=pw.email("ssofailmember"), email_verified=True, name="S", groups=())
    assert await pw.run(org, lambda s: auth_service._resolve_or_provision_user(s, organization_id=org, idp_subject=subject, idp_claims=claims)) == member


@pytest.mark.asyncio
async def test_the_raw_token_is_not_recoverable_from_the_database(pw) -> None:  # noqa: F811
    org = await _org(pw, "hash")
    await pw.sup("INSERT INTO scim_configurations (organization_id, enabled) VALUES (:o, true)", o=org)
    from app.core.users import service as users_service

    admin = await _user(pw, "hashadmin", org)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL ON CONFLICT DO NOTHING",
        u=admin, o=org,
    )
    actor = await pw.run(org, lambda s: users_service.resolve_identity(s, admin, org))
    row, raw = await pw.run(org, lambda s: tokens.create_token(s, actor, name="N", ttl_days=7))
    dump = str(await pw.sup("SELECT to_jsonb(t) FROM scim_tokens t WHERE id=:i", i=row.id))
    assert raw not in dump, "the raw token must not be stored"
    assert row.token_hash == tokens.hash_token(raw), (row.token_hash, tokens.hash_token(raw))
    stored = (await pw.sup("SELECT token_hash FROM scim_tokens WHERE id=:i", i=row.id))[0][0]
    assert stored == tokens.hash_token(raw) and stored != raw
    assert len(row.token_prefix) == 12
    assert row.expires_at > datetime.now(UTC) + timedelta(days=6)
    audit = str(await pw.sup("SELECT metadata::text FROM audit_logs WHERE organization_id=:o AND action='scim.token_created'", o=org))
    assert raw not in audit
