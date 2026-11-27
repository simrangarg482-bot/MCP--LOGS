"""Migration `a1d4e7c2b9f3` (`sso_configurations.enabled`) on real Postgres.

Own throw-away database on a LOCAL server (same contract and helpers as the
other migration tests): replay history to the previous revision, create an SSO
configuration plus an identity link the way the previous release did, apply the
migration and prove nothing existing was rewritten and that the existing
configuration keeps working (`enabled = true`); then the downgrade round trip.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from tests.database.test_roles_migration_postgres import (  # noqa: F401  (helpers + skip mark)
    _alembic,
    _dsn,
    _exec,
    _fetch,
    pytestmark,
)

_PREVIOUS = "f4b8d1c7a3e9"
_REVISION = "a1d4e7c2b9f3"


@pytest.fixture(scope="module")
def database():
    name = f"ekip_mig_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    yield name
    asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


_LEGACY = """
INSERT INTO organizations (name, slug, status) VALUES ('Org', 'org-sso', 'active');
INSERT INTO users (email, display_name, is_active, email_verified_at) VALUES ('u@x.test','U',true,now());
INSERT INTO sso_configurations (organization_id, provider, protocol, issuer_url, client_id, client_secret_ref)
    SELECT id, 'okta', 'oidc', 'https://acme.okta.com', 'client-1', 'ciphertext-blob' FROM organizations WHERE slug='org-sso';
INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject)
    SELECT o.id, u.id, 'sub-1' FROM organizations o, users u WHERE o.slug='org-sso' AND u.email='u@x.test';
"""


def test_upgrade_keeps_existing_configuration_and_enables_it(database) -> None:
    up = _alembic(database, "upgrade", _PREVIOUS)
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(_exec(database, _LEGACY))
    before = asyncio.run(
        _fetch(database, "SELECT id, provider, protocol, issuer_url, client_id, client_secret_ref, updated_at FROM sso_configurations")
    )

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = await _fetch(
            database,
            "SELECT id, provider, protocol, issuer_url, client_id, client_secret_ref, updated_at, enabled "
            "FROM sso_configurations",
        )
        assert len(after) == 1
        old, new = before[0], after[0]
        assert tuple(new)[:7] == tuple(old)[:7]  # nothing rewritten, secret untouched
        assert new["enabled"] is True  # an existing SSO setup keeps working
        assert (await _fetch(database, "SELECT count(*) AS n FROM external_identity_mappings"))[0]["n"] == 1
        col = await _fetch(
            database,
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name='sso_configurations' AND column_name='enabled'",
        )
        assert col[0]["is_nullable"] == "NO" and "true" in col[0]["column_default"]
        await _exec(database, "UPDATE sso_configurations SET enabled = false")

    asyncio.run(check())


def test_downgrade_and_reapply_round_trip(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]
    cols = asyncio.run(
        _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='sso_configurations'")
    )
    assert "enabled" not in {c["column_name"] for c in cols}
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM sso_configurations"))[0]["n"] == 1  # data kept

    again = _alembic(database, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    assert asyncio.run(_fetch(database, "SELECT enabled FROM sso_configurations"))[0]["enabled"] is True
