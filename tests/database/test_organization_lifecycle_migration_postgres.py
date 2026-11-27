"""Migration `d4a7b1c8e3f6` (organization lifecycle) on real Postgres, in its own throw-away database on a
LOCAL server (same contract as the other migration tests).

Covers: nothing existing is rewritten and nobody is promoted; invalid existing statuses make the migration
STOP with a clear error that names them and changes nothing; the constraint ends up validated; the new
columns/tables/functions/privileges exist; the downgrade removes them without touching organization data;
and the upgrade/downgrade round trip is clean."""

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

_PREVIOUS = "b2c5e8a1d4f7"
_REVISION = "d4a7b1c8e3f6"


def _new_database():
    name = f"ekip_mig_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    return name, lambda: asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def database():
    name, drop = _new_database()
    yield name
    drop()


@pytest.fixture
def scratch():
    name, drop = _new_database()
    yield name
    drop()


_ROWS = """
INSERT INTO organizations (name, slug, status) VALUES
    ('A', 'org-a', 'onboarding'), ('B', 'org-b', 'active'), ('C', 'org-c', 'suspended');
INSERT INTO users (email, display_name, is_active, email_verified_at)
    VALUES ('u@x.test','U',true,now());
INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at)
    SELECT u.id, o.id, gen_random_uuid(), 'rt-' || o.slug, now() + interval '30 days'
    FROM users u, organizations o WHERE u.email='u@x.test';
"""


def test_upgrade_is_additive_changes_no_status_and_ends_no_session(database) -> None:
    up = _alembic(database, "upgrade", _PREVIOUS)
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(_exec(database, _ROWS))
    before = asyncio.run(_fetch(database, "SELECT id, name, slug, status, created_at, updated_at FROM organizations ORDER BY slug"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = await _fetch(
            database,
            "SELECT id, name, slug, status, created_at, updated_at, status_changed_at, status_message_public, session_epoch "
            "FROM organizations ORDER BY slug",
        )
        assert [tuple(r)[:6] for r in after] == [tuple(r) for r in before]  # nothing rewritten, nobody promoted
        assert [r["status"] for r in after] == ["onboarding", "active", "suspended"]
        assert all(r["status_changed_at"] is None and r["status_message_public"] is None and r["session_epoch"] == 0 for r in after)
        # existing sessions are not touched (the epoch of an existing org is 0, as is the claim of an old token)
        assert (await _fetch(database, "SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL"))[0]["n"] == 3

        column = (await _fetch(database, "SELECT is_nullable, column_default FROM information_schema.columns WHERE table_name='organizations' AND column_name='status'"))[0]
        assert column["is_nullable"] == "NO" and "'onboarding'" in column["column_default"]
        assert (await _fetch(database, "SELECT convalidated AS v FROM pg_constraint WHERE conname='ck_organizations_status'"))[0]["v"] is True
        for table in ("platform_operators", "organization_status_history", "platform_audit_events"):
            assert await _fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name=$1", table)
            assert (await _fetch(database, "SELECT relrowsecurity AS r FROM pg_class WHERE relname=$1", table))[0]["r"] is True
        assert {r["tgname"] for r in await _fetch(database, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")} >= {
            "organizations_status_lifecycle",
            "organization_status_history_append_only",
            "platform_audit_events_append_only",
        }
        # the application role's rights: no unrestricted organization UPDATE, no platform table access
        has = lambda sql: _fetch(database, sql)  # noqa: E731
        assert (await has("SELECT has_column_privilege('ekip_app','organizations','status','UPDATE') AS p"))[0]["p"] is False
        assert (await has("SELECT has_column_privilege('ekip_app','organizations','name','UPDATE') AS p"))[0]["p"] is True
        assert (await has("SELECT has_table_privilege('ekip_app','organizations','INSERT') AS p"))[0]["p"] is True
        for table in ("platform_operators", "organization_status_history", "platform_audit_events"):
            assert (await has(f"SELECT has_table_privilege('ekip_app','{table}','SELECT,INSERT,UPDATE,DELETE') AS p"))[0]["p"] is False

    asyncio.run(check())


def test_downgrade_removes_the_lifecycle_objects_and_keeps_organization_data(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]

    async def check():
        cols = {c["column_name"] for c in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='organizations'")}
        assert not cols & {"status_changed_at", "status_message_public", "session_epoch"}
        assert not await _fetch(database, "SELECT 1 FROM pg_constraint WHERE conname='ck_organizations_status'")
        for table in ("platform_operators", "organization_status_history", "platform_audit_events"):
            assert not await _fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name=$1", table)
        assert not await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname LIKE 'platform_%' OR proname='organizations_status_lifecycle'")
        assert not await _fetch(database, "SELECT 1 FROM pg_trigger WHERE tgname='organizations_status_lifecycle'")
        assert [r["status"] for r in await _fetch(database, "SELECT status FROM organizations ORDER BY slug")] == ["onboarding", "active", "suspended"]
        # the application role can update organizations again, as before this migration
        assert (await _fetch(database, "SELECT has_column_privilege('ekip_app','organizations','status','UPDATE') AS p"))[0]["p"] is True

    asyncio.run(check())

    again = _alembic(database, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 3


@pytest.mark.parametrize("bad", ["trial", "Active", " suspended", "deleted", ""])
def test_an_unknown_existing_status_stops_the_migration_and_changes_nothing(scratch, bad) -> None:
    assert _alembic(scratch, "upgrade", _PREVIOUS).returncode == 0
    asyncio.run(_exec(scratch, "INSERT INTO organizations (name, slug, status) VALUES ('ok','ok','active'), ('bad','bad',$1)", bad))

    result = _alembic(scratch, "upgrade", _REVISION)

    assert result.returncode != 0
    assert "outside (onboarding, active, suspended)" in result.stderr + result.stdout
    assert repr(bad) in result.stderr + result.stdout or f"'{bad}'" in result.stderr + result.stdout
    assert "never converts an unknown status" in result.stderr + result.stdout

    async def check():
        # nothing was changed: still at the previous revision, no new objects, the bad row is untouched
        assert (await _fetch(scratch, "SELECT version_num FROM alembic_version"))[0]["version_num"] == _PREVIOUS
        cols = {c["column_name"] for c in await _fetch(scratch, "SELECT column_name FROM information_schema.columns WHERE table_name='organizations'")}
        assert "session_epoch" not in cols
        assert not await _fetch(scratch, "SELECT 1 FROM pg_constraint WHERE conname='ck_organizations_status'")
        assert not await _fetch(scratch, "SELECT 1 FROM information_schema.tables WHERE table_name='platform_operators'")
        assert {r["status"] for r in await _fetch(scratch, "SELECT status FROM organizations")} == {"active", bad}

    asyncio.run(check())

    # after an operator corrects the data explicitly, the very same migration succeeds
    asyncio.run(_exec(scratch, "UPDATE organizations SET status='onboarding' WHERE slug='bad'"))
    assert _alembic(scratch, "upgrade", _REVISION).returncode == 0
    assert asyncio.run(_fetch(scratch, "SELECT status FROM organizations WHERE slug='bad'"))[0]["status"] == "onboarding"


def test_a_null_status_cannot_exist_so_nothing_to_convert(scratch) -> None:
    assert _alembic(scratch, "upgrade", _REVISION).returncode == 0
    with pytest.raises(asyncpg.NotNullViolationError):
        asyncio.run(_exec(scratch, "INSERT INTO organizations (name, slug, status) VALUES ('n','n',NULL)"))
