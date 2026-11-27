"""Migration `b2c5e8a1d4f7` (password_resets, users.sessions_valid_after, revoke_user_refresh_tokens)
on real Postgres, in its own throw-away database on a LOCAL server (same contract as the other
migration tests): nothing existing is rewritten, no session is affected, constraints and the
SECURITY DEFINER function behave, and the downgrade/upgrade round trip is clean."""

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

_PREVIOUS = "a1d4e7c2b9f3"
_REVISION = "b2c5e8a1d4f7"


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
INSERT INTO organizations (name, slug, status) VALUES ('Org', 'org-pw', 'active');
INSERT INTO users (email, display_name, is_active, email_verified_at, password_hash)
    VALUES ('u@x.test','U',true,now(),'$2b$12$legacylegacylegacylegacyleLeg.legacylegacylegacylegacyle');
INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at)
    SELECT u.id, o.id, gen_random_uuid(), 'legacy-hash', now() + interval '30 days'
    FROM users u, organizations o WHERE u.email='u@x.test' AND o.slug='org-pw';
"""


def test_upgrade_is_additive_and_ends_nobody_s_session(database) -> None:
    up = _alembic(database, "upgrade", _PREVIOUS)
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(_exec(database, _LEGACY))
    before = asyncio.run(_fetch(database, "SELECT id, password_hash, email_verified_at, updated_at FROM users"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = await _fetch(database, "SELECT id, password_hash, email_verified_at, updated_at, sessions_valid_after FROM users")
        old, new = before[0], after[0]
        assert tuple(new)[:4] == tuple(old)[:4]  # nothing rewritten (the legacy bcrypt-style hash is kept as-is)
        assert new["sessions_valid_after"] is None  # no existing access token is affected
        assert (await _fetch(database, "SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL"))[0]["n"] == 1
        assert (await _fetch(database, "SELECT count(*) AS n FROM password_resets"))[0]["n"] == 0
        # one OPEN link per user, token hashes unique, cascade on user delete
        uid = new["id"]
        await _exec(database, "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES ($1,'h1', now())", uid)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _exec(database, "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES ($1,'h2', now())", uid)
        await _exec(database, "UPDATE password_resets SET consumed_at = now()")
        await _exec(database, "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES ($1,'h3', now())", uid)  # allowed once the first is consumed
        with pytest.raises(asyncpg.UniqueViolationError):
            await _exec(database, "INSERT INTO password_resets (user_id, token_hash, expires_at, consumed_at) VALUES ($1,'h3', now(), now())", uid)
        # the revocation function only sets revoked_at on that user's valid tokens and returns a count
        assert (await _fetch(database, "SELECT revoke_user_refresh_tokens($1, now()) AS n", uid))[0]["n"] == 1
        assert (await _fetch(database, "SELECT revoke_user_refresh_tokens($1, now()) AS n", uid))[0]["n"] == 0
        assert (await _fetch(database, "SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NOT NULL"))[0]["n"] == 1
        # PUBLIC may not execute it
        assert (await _fetch(database, "SELECT has_function_privilege('public', 'revoke_user_refresh_tokens(uuid, timestamptz)', 'EXECUTE') AS ok"))[0]["ok"] is False

    asyncio.run(check())


def test_downgrade_and_reapply_round_trip(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]
    cols = asyncio.run(_fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='users'"))
    assert "sessions_valid_after" not in {c["column_name"] for c in cols}
    assert not asyncio.run(_fetch(database, "SELECT 1 FROM pg_proc WHERE proname='revoke_user_refresh_tokens'"))
    assert not asyncio.run(_fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name='password_resets'"))
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM users"))[0]["n"] == 1  # data kept

    again = _alembic(database, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM password_resets"))[0]["n"] == 0
