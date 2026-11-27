"""Migration `e8c3a1f5b7d2` (organization-scoped custom roles) on real Postgres.

Builds its own throw-away database on a LOCAL server, replays the history up to
the previous revision, seeds "legacy" rows, applies the migration and checks
what must be preserved, what must be created and what must be enforced; then
checks the downgrade refuses to destroy custom roles and is otherwise clean.

Skipped unless `EKIP_TEST_POSTGRES_URL` points at a local superuser connection
(same variable as the other `*_postgres` tests) and `EKIP_APP_ROLE_PASSWORD`
is set for the `ekip_app` provisioning migration. The database it creates is
dropped at the end; nothing else on the server is touched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

_SUPER_URL = os.environ.get("EKIP_TEST_POSTGRES_URL", "")
_PREVIOUS = "a8d4f2c6e1b9"
_REVISION = "e8c3a1f5b7d2"
_ROOT = Path(__file__).resolve().parents[2]


def _parts(url: str):
    return urlsplit(url.replace("+asyncpg", ""))


pytestmark = pytest.mark.skipif(
    not (
        _SUPER_URL
        and (_parts(_SUPER_URL).hostname or "") in {"localhost", "127.0.0.1", "::1"}
        and os.environ.get("EKIP_APP_ROLE_PASSWORD")
    ),
    reason="needs a LOCAL EKIP_TEST_POSTGRES_URL (superuser) and EKIP_APP_ROLE_PASSWORD",
)


def _dsn(database: str) -> str:
    p = _parts(_SUPER_URL)
    return f"postgresql://{p.username}:{p.password or ''}@{p.hostname}:{p.port}/{database}"


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    url = _dsn(database).replace("postgresql://", "postgresql+asyncpg://") + "?sslmode=disable"
    env.update(MIGRATION_DATABASE_URL=url, DATABASE_URL=url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=900,
    )


async def _fetch(database: str, sql: str, *params):
    conn = await asyncpg.connect(_dsn(database))
    try:
        return await conn.fetch(sql, *params)
    finally:
        await conn.close()


async def _exec(database: str, sql: str, *params):
    conn = await asyncpg.connect(_dsn(database))
    try:
        return await conn.execute(sql, *params)
    finally:
        await conn.close()


_LEGACY = """
INSERT INTO permissions (code, description) VALUES ('tenancy:manage', NULL), ('incident:write', 'kept')
    ON CONFLICT (code) DO NOTHING;
INSERT INTO roles (name, description) VALUES ('admin', 'legacy admin');
INSERT INTO role_permissions SELECT r.id, p.id FROM roles r, permissions p
    WHERE r.name='admin' AND p.code IN ('tenancy:manage', 'incident:write');
INSERT INTO organizations (name, slug, status) VALUES ('Org A','org-a','active'), ('Org B','org-b','active');
INSERT INTO users (email, display_name, is_active, email_verified_at)
    VALUES ('alice@a.test','Alice',true,now()), ('bob@b.test','Bob',true,now());
INSERT INTO projects (organization_id, name, is_default) SELECT id,'General',true FROM organizations;
INSERT INTO user_roles SELECT u.id, o.id, r.id FROM users u JOIN organizations o
    ON (u.email='alice@a.test' AND o.slug='org-a') OR (u.email='bob@b.test' AND o.slug='org-b')
    CROSS JOIN roles r WHERE r.name='admin';
INSERT INTO invitations (organization_id,email,status,grants_role_id,invited_by,expires_at)
    SELECT o.id,'new@a.test','pending',r.id,u.id,now()+interval '3 days'
    FROM organizations o, roles r, users u WHERE o.slug='org-a' AND r.name='admin' AND u.email='alice@a.test';
INSERT INTO organization_access_rules (organization_id,rule_type,value,grants_role_id,is_active)
    SELECT o.id,'domain','a.test',r.id,true FROM organizations o, roles r WHERE o.slug='org-a' AND r.name='admin';
INSERT INTO project_memberships SELECT u.id, p.id, r.id FROM users u, projects p, roles r, organizations o
    WHERE u.email='alice@a.test' AND p.organization_id=o.id AND o.slug='org-a' AND r.name='admin';
"""


@pytest.fixture(scope="module")
def database():
    name = f"ekip_mig_{uuid.uuid4().hex[:8]}"

    async def create():
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def drop():
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    asyncio.run(create())
    yield name
    asyncio.run(drop())


def test_upgrade_preserves_legacy_data_and_creates_the_new_structure(database) -> None:
    up = _alembic(database, "upgrade", _PREVIOUS)
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(_exec(database, _LEGACY))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        roles = await _fetch(
            database,
            "SELECT coalesce(o.slug,'<builtin>') AS org, r.name, "
            "(SELECT count(*) FROM role_permissions rp WHERE rp.role_id=r.id) AS perms "
            "FROM roles r LEFT JOIN organizations o ON o.id=r.organization_id ORDER BY 1,2",
        )
        by = {(r["org"], r["name"]): r["perms"] for r in roles}
        # the legacy admin is now the built-in role, with every catalog permission
        assert by[("<builtin>", "admin")] == 8
        for org in ("org-a", "org-b"):  # starter roles for existing organizations
            assert by[(org, "viewer")] == 2
            assert by[(org, "engineer")] == 4
            assert by[(org, "reviewer")] == 4
        assert len(by) == 7
        # nothing existing was lost or re-pointed
        for table, expected in (
            ("user_roles", 2), ("invitations", 1), ("organization_access_rules", 1), ("project_memberships", 1)
        ):
            assert (await _fetch(database, f"SELECT count(*) AS n FROM {table}"))[0]["n"] == expected
        legacy_desc = await _fetch(database, "SELECT description FROM permissions WHERE code='incident:write'")
        assert legacy_desc[0]["description"] == "kept"  # existing descriptions are not overwritten
        rls = await _fetch(
            database,
            "SELECT relname, relrowsecurity AS on_, relforcerowsecurity AS forced FROM pg_class "
            "WHERE relname IN ('roles','role_permissions')",
        )
        assert all(r["on_"] and r["forced"] for r in rls) and len(rls) == 2
        # case-insensitive uniqueness per scope
        with pytest.raises(asyncpg.UniqueViolationError):
            await _exec(database, "INSERT INTO roles (name) VALUES ('ADMIN')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _exec(
                database,
                "INSERT INTO roles (organization_id, name) SELECT id, 'VIEWER' FROM organizations WHERE slug='org-a'",
            )
        # ...but the same name in another organization is fine
        await _exec(
            database,
            "INSERT INTO roles (organization_id, name) SELECT id, 'shared-name' FROM organizations",
        )
        await _exec(database, "DELETE FROM roles WHERE name='shared-name'")

    asyncio.run(check())


def test_the_migration_is_idempotent_to_rerun_after_downgrade_and_refuses_to_destroy_roles(database) -> None:
    # a custom role makes the downgrade refuse (it would cascade-delete assignments)
    asyncio.run(
        _exec(
            database,
            "INSERT INTO roles (organization_id, name) SELECT id, 'keep-me' FROM organizations WHERE slug='org-a'",
        )
    )
    refused = _alembic(database, "downgrade", _PREVIOUS)
    assert refused.returncode != 0
    assert "Refusing to downgrade" in (refused.stderr + refused.stdout)
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM roles WHERE name='keep-me'"))[0]["n"] == 1

    # with the custom roles gone the downgrade is clean and restores the old shape
    asyncio.run(_exec(database, "DELETE FROM roles WHERE organization_id IS NOT NULL"))
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]
    cols = asyncio.run(
        _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='roles'")
    )
    assert "organization_id" not in {c["column_name"] for c in cols}
    assert asyncio.run(
        _fetch(database, "SELECT count(*) AS n FROM pg_policies WHERE tablename IN ('roles','role_permissions')")
    )[0]["n"] == 0
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM user_roles"))[0]["n"] == 2

    # and re-applying works and recreates the starter roles
    again = _alembic(database, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    n = asyncio.run(_fetch(database, "SELECT count(*) AS n FROM roles WHERE organization_id IS NOT NULL"))
    assert n[0]["n"] == 6
