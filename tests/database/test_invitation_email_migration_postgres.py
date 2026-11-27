"""Migration `f4b8d1c7a3e9` (invitation email delivery tracking) on real Postgres.

Builds its own throw-away database on a LOCAL server (same contract and helpers
as `test_roles_migration_postgres.py`): replays history to the previous
revision, creates an invitation the way an older release did, applies the
migration and checks that nothing existing was rewritten, the new columns and
constraint behave, and the downgrade/upgrade round trip is clean.
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

_PREVIOUS = "e8c3a1f5b7d2"
_REVISION = "f4b8d1c7a3e9"


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


_LEGACY_INVITATION = """
INSERT INTO organizations (name, slug, status) VALUES ('Org', 'org-x', 'active');
INSERT INTO users (email, display_name, is_active, email_verified_at) VALUES ('admin@x.test','Admin',true,now());
INSERT INTO invitations (organization_id, email, status, grants_role_id, invited_by, expires_at, token_hash)
    SELECT o.id, 'old@x.test', 'pending', r.id, u.id, now() + interval '3 days', 'legacy-hash'
    FROM organizations o, roles r, users u WHERE o.slug='org-x' AND r.name='admin' AND u.email='admin@x.test';
"""


def test_upgrade_is_additive_and_defaults_existing_rows(database) -> None:
    up = _alembic(database, "upgrade", _PREVIOUS)
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(_exec(database, _LEGACY_INVITATION))
    before = asyncio.run(_fetch(database, "SELECT id, token_hash, status, expires_at, updated_at FROM invitations"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = await _fetch(
            database,
            "SELECT id, token_hash, status, expires_at, updated_at, email_status, email_attempts, "
            "email_sent_at, email_last_attempt_at, resend_count FROM invitations",
        )
        assert len(after) == 1
        row, old = after[0], before[0]
        # nothing that existed was rewritten
        assert (row["id"], row["token_hash"], row["status"], row["expires_at"], row["updated_at"]) == (
            old["id"], old["token_hash"], old["status"], old["expires_at"], old["updated_at"]
        )
        # legacy invitations were handed over by copying the link
        assert row["email_status"] == "not_sent" and row["email_attempts"] == 0 and row["resend_count"] == 0
        assert row["email_sent_at"] is None and row["email_last_attempt_at"] is None

        with pytest.raises(asyncpg.CheckViolationError):
            await _exec(database, "UPDATE invitations SET email_status = 'delivered'")
        for status in ("queued", "sent", "failed", "not_configured", "not_sent"):
            await _exec(database, "UPDATE invitations SET email_status = $1", status)
        index = await _fetch(database, "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_invitations_org_created'")
        assert index

    asyncio.run(check())


def test_downgrade_and_reapply_round_trip(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]
    cols = asyncio.run(
        _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='invitations'")
    )
    assert {"email_status", "resend_count", "email_attempts"}.isdisjoint({c["column_name"] for c in cols})
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM invitations"))[0]["n"] == 1  # data kept

    again = _alembic(database, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    assert asyncio.run(_fetch(database, "SELECT email_status FROM invitations"))[0]["email_status"] == "not_sent"
