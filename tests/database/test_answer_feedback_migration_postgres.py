"""Migration `d3b7f1a9c5e2` (answer_feedback) and the feedback feature's
database-level security guarantees, on REAL Postgres in a throw-away database
on a LOCAL server.

What this proves that mocks cannot:
  * the migration is additive -- existing executions are untouched;
  * row-level security is enabled AND forced, and actually isolates
    organizations when queried as the least-privileged application role;
  * a feedback row cannot reference another organization's execution (the
    composite foreign key), whatever the application does;
  * the unique / CHECK constraints hold, and cascades behave;
  * the real repository's single-statement upsert converges under
    concurrency, and the real service refuses a cross-organization rating
    through real RLS.

Skipped unless `EKIP_TEST_POSTGRES_URL` points at a local superuser connection
and `EKIP_APP_ROLE_PASSWORD` is set (same contract as the other `*_postgres`
tests). The database it creates is dropped at the end.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.database.test_roles_migration_postgres import (  # noqa: F401  (helpers + skip mark)
    _SUPER_URL,
    _alembic,
    _dsn,
    _exec,
    _fetch,
    _parts,
    pytestmark,
)

_PREVIOUS = "e1c7a9d3b5f2"
_REVISION = "d3b7f1a9c5e2"
_GUC = "app.current_organization_id"


@pytest.fixture(scope="module")
def database():
    name = f"ekip_fb_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    yield name
    asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


_SEED = """
INSERT INTO organizations (name, slug, status) VALUES ('Org A','org-a','active'), ('Org B','org-b','active');
INSERT INTO users (email, display_name, is_active, email_verified_at)
    VALUES ('alice@a.test','Alice',true,now()), ('bob@b.test','Bob',true,now());
INSERT INTO agent_executions (organization_id, user_id, agent_name, trigger_source, status)
    SELECT o.id, u.id, 'answer_question', 'core_api', 'succeeded'
    FROM organizations o JOIN users u ON (o.slug='org-a' AND u.email='alice@a.test')
                                      OR (o.slug='org-b' AND u.email='bob@b.test');
"""


async def _ids(database: str) -> dict[str, uuid.UUID]:
    rows = await _fetch(
        database,
        "SELECT o.slug AS slug, o.id AS org, u.id AS usr, e.id AS exe FROM organizations o "
        "JOIN agent_executions e ON e.organization_id = o.id JOIN users u ON u.id = e.user_id",
    )
    ids: dict[str, uuid.UUID] = {}
    for row in rows:
        side = row["slug"][-1]  # 'a' / 'b'
        ids.update({f"org_{side}": row["org"], f"user_{side}": row["usr"], f"exec_{side}": row["exe"]})
    return ids


def _app_dsn(database: str) -> str:
    p = _parts(_SUPER_URL)
    return f"postgresql://ekip_app:{os.environ['EKIP_APP_ROLE_PASSWORD']}@{p.hostname}:{p.port}/{database}"


async def _as_app(database: str, org: uuid.UUID | None, fn):
    """Run `fn(conn)` as the least-privileged `ekip_app` role in a transaction,
    with the tenant GUC set to `org` (or never set, for `None`)."""
    conn = await asyncpg.connect(_app_dsn(database))
    try:
        async with conn.transaction():
            if org is not None:
                await conn.execute("SELECT set_config($1, $2, true)", _GUC, str(org))
            return await fn(conn)
    finally:
        await conn.close()


def test_upgrade_is_additive_and_creates_a_forced_rls_table(database) -> None:
    assert _alembic(database, "upgrade", _PREVIOUS).returncode == 0
    asyncio.run(_exec(database, _SEED))
    before = asyncio.run(_fetch(database, "SELECT id, organization_id, user_id, status FROM agent_executions ORDER BY id"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = await _fetch(database, "SELECT id, organization_id, user_id, status, output_summary FROM agent_executions ORDER BY id")
        assert [tuple(r)[:4] for r in after] == [tuple(r) for r in before]  # nothing changed
        assert all(r["output_summary"] is None for r in after)  # nullable, unfilled

        rls = (await _fetch(database, "SELECT relrowsecurity AS r, relforcerowsecurity AS f FROM pg_class WHERE relname='answer_feedback'"))[0]
        assert rls["r"] is True and rls["f"] is True
        policies = await _fetch(database, "SELECT policyname FROM pg_policies WHERE tablename='answer_feedback'")
        assert [p["policyname"] for p in policies] == ["tenant_isolation"]
        assert (await _fetch(database, "SELECT count(*) AS n FROM answer_feedback"))[0]["n"] == 0

        constraints = {
            r["conname"]
            for r in await _fetch(database, "SELECT conname FROM pg_constraint WHERE conrelid = 'answer_feedback'::regclass")
        }
        assert {
            "uq_answer_feedback_execution_user",
            "fk_answer_feedback_execution_same_organization",
            "ck_answer_feedback_rating",
            "ck_answer_feedback_comment_length",
        } <= constraints
        indexes = {r["indexname"] for r in await _fetch(database, "SELECT indexname FROM pg_indexes WHERE tablename='answer_feedback'")}
        assert {"ix_answer_feedback_org_created_at", "ix_answer_feedback_org_rating", "ix_answer_feedback_user_id"} <= indexes
        assert (await _fetch(database, "SELECT 1 FROM pg_constraint WHERE conname='uq_agent_executions_id_organization'"))
        # the application role can read/write it (default privileges) ...
        priv = await _fetch(database, "SELECT has_table_privilege('ekip_app','answer_feedback','SELECT,INSERT,UPDATE,DELETE') AS p")
        assert priv[0]["p"] is True

    asyncio.run(check())


def test_database_constraints(database) -> None:
    ids = asyncio.run(_ids(database))

    async def check():
        conn = await asyncpg.connect(_dsn(database))  # superuser: isolates constraints from RLS
        insert = (
            "INSERT INTO answer_feedback (organization_id, execution_id, user_id, rating, comment) "
            "VALUES ($1,$2,$3,$4,$5)"
        )
        try:
            await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_a"], "helpful", None)

            # one rating per (execution, user)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_a"], "not_helpful", None)
            # rating vocabulary
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_b"], "great", None)
            # comment length cap
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_b"], "helpful", "x" * 2001)
            # CROSS-ORGANIZATION EXECUTION REFERENCE: org B claiming org A's execution
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(insert, ids["org_b"], ids["exec_a"], ids["user_b"], "not_helpful", None)
            # ... and org A claiming org B's execution
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(insert, ids["org_a"], ids["exec_b"], ids["user_a"], "not_helpful", None)
            # a nonexistent execution
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(insert, ids["org_a"], uuid.uuid4(), ids["user_a"], "helpful", None)

            # a legitimate second user's rating of the same execution is fine
            await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_b"], "not_helpful", None)
            assert await conn.fetchval("SELECT count(*) FROM answer_feedback WHERE execution_id=$1", ids["exec_a"]) == 2

            # deleting the execution removes the ratings of it (CASCADE) ...
            await conn.execute("DELETE FROM answer_feedback WHERE execution_id=$1", ids["exec_a"])
            await conn.execute(insert, ids["org_a"], ids["exec_a"], ids["user_a"], "helpful", None)
            await conn.execute("DELETE FROM agent_executions WHERE id=$1", ids["exec_a"])
            assert await conn.fetchval("SELECT count(*) FROM answer_feedback") == 0
            # ... restore it for the later tests
            await conn.execute(
                "INSERT INTO agent_executions (id, organization_id, user_id, agent_name, trigger_source, status) "
                "VALUES ($1,$2,$3,'answer_question','core_api','succeeded')",
                ids["exec_a"], ids["org_a"], ids["user_a"],
            )
        finally:
            await conn.close()

    asyncio.run(check())


def test_rls_isolates_organizations_for_the_application_role(database) -> None:
    ids = asyncio.run(_ids(database))

    async def seed():
        conn = await asyncpg.connect(_dsn(database))
        try:
            await conn.execute(
                "INSERT INTO answer_feedback (organization_id, execution_id, user_id, rating) VALUES "
                "($1,$2,$3,'helpful'), ($4,$5,$6,'not_helpful')",
                ids["org_a"], ids["exec_a"], ids["user_a"], ids["org_b"], ids["exec_b"], ids["user_b"],
            )
        finally:
            await conn.close()

    asyncio.run(seed())

    async def visible(org):
        async def fn(conn):
            return sorted(r["rating"] for r in await conn.fetch("SELECT rating FROM answer_feedback"))

        return await _as_app(database, org, fn)

    async def check():
        assert await visible(ids["org_a"]) == ["helpful"]
        assert await visible(ids["org_b"]) == ["not_helpful"]
        assert await visible(None) == []  # no tenant context: nothing, not everything
        assert await visible(uuid.uuid4()) == []  # an unrelated tenant sees nothing

        # a tenant cannot write a row for another tenant ...
        async def forge(conn):
            await conn.execute(
                "INSERT INTO answer_feedback (organization_id, execution_id, user_id, rating) VALUES ($1,$2,$3,'helpful')",
                ids["org_b"], ids["exec_b"], ids["user_a"],
            )

        with pytest.raises(asyncpg.PostgresError) as forged:
            await _as_app(database, ids["org_a"], forge)
        assert "row-level security" in str(forged.value)

        # ... nor modify or delete another tenant's rows (zero rows affected, no error)
        async def tamper(conn):
            updated = await conn.execute("UPDATE answer_feedback SET rating='helpful' WHERE organization_id=$1", ids["org_b"])
            deleted = await conn.execute("DELETE FROM answer_feedback WHERE organization_id=$1", ids["org_b"])
            return updated, deleted

        assert await _as_app(database, ids["org_a"], tamper) == ("UPDATE 0", "DELETE 0")
        assert await visible(ids["org_b"]) == ["not_helpful"]

    asyncio.run(check())


def test_real_repository_and_service_under_rls(database) -> None:
    """The actual code, against the actual database, as the actual role."""
    from app.agents.feedback import repository, service
    from app.agents.feedback.schemas import FeedbackSubmit
    from app.core.exceptions import NotFoundError
    from app.database.session import set_tenant_context
    from app.shared.schemas import ActorKind, Identity

    ids = asyncio.run(_ids(database))
    engine = create_async_engine(
        _app_dsn(database).replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"ssl": False},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def actor(side: str) -> Identity:
        return Identity(
            kind=ActorKind.USER,
            subject=str(ids[f"user_{side}"]),
            organization_id=ids[f"org_{side}"],
            user_id=ids[f"user_{side}"],
        )

    async def submit(side: str, execution_id: uuid.UUID, **body):
        async with sessions() as session:
            await set_tenant_context(session, ids[f"org_{side}"])
            result = await service.submit_feedback(session, actor(side), execution_id, FeedbackSubmit(**body))
            await session.commit()
            return result

    async def check():
        try:
            # start clean
            conn = await asyncpg.connect(_dsn(database))
            await conn.execute("DELETE FROM answer_feedback")
            await conn.close()

            first = await submit("a", ids["exec_a"], rating="helpful")
            changed = await submit("a", ids["exec_a"], rating="not_helpful", reason_code="outdated", comment="old")
            assert changed.id == first.id  # updated in place
            assert changed.created_at == first.created_at
            assert changed.updated_at >= first.updated_at
            assert (changed.rating, changed.reason_code, changed.comment) == ("not_helpful", "outdated", "old")

            # the audit trail recorded a submit then an update, without the comment text
            audit = await _fetch(
                database,
                "SELECT action, metadata::text AS m FROM audit_logs WHERE resource_type='answer_feedback' ORDER BY occurred_at",
            )
            assert [r["action"] for r in audit] == ["answer_feedback.submit", "answer_feedback.update"]
            assert all("old" not in r["m"].replace("not_helpful", "") for r in audit)

            # concurrent identical submissions converge on exactly one row
            await asyncio.gather(*[submit("a", ids["exec_a"], rating="helpful") for _ in range(8)])
            count = await _fetch(database, "SELECT count(*) AS n FROM answer_feedback WHERE execution_id=$1", ids["exec_a"])
            assert count[0]["n"] == 1

            # CROSS-ORGANIZATION: org B cannot rate org A's execution -- reported as not found
            with pytest.raises(NotFoundError):
                await submit("b", ids["exec_a"], rating="not_helpful")
            assert (await _fetch(database, "SELECT count(*) AS n FROM answer_feedback WHERE organization_id=$1", ids["org_b"]))[0]["n"] == 0

            # and org B cannot READ org A's rating either
            async with sessions() as session:
                await set_tenant_context(session, ids["org_b"])
                assert await service.get_my_feedback(session, actor("b"), ids["exec_a"]) is None
                assert await repository.get_execution(
                    session, organization_id=ids["org_b"], execution_id=ids["exec_a"]
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_downgrade_removes_only_this_feature_and_a_reupgrade_is_clean(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]

    async def after_down():
        assert not await _fetch(database, "SELECT 1 FROM pg_class WHERE relname='answer_feedback'")
        assert not await _fetch(database, "SELECT 1 FROM pg_constraint WHERE conname='uq_agent_executions_id_organization'")
        cols = {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='agent_executions'")}
        assert "output_summary" not in cols
        assert (await _fetch(database, "SELECT count(*) AS n FROM agent_executions"))[0]["n"] == 2  # executions survive

    asyncio.run(after_down())

    up = _alembic(database, "upgrade", _REVISION)
    assert up.returncode == 0, up.stderr[-2000:]
    assert (asyncio.run(_fetch(database, "SELECT count(*) AS n FROM answer_feedback")))[0]["n"] == 0
