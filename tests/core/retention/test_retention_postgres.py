"""Scheduled retention against real PostgreSQL as `ekip_app` (RLS enforced): exactly the expired records go, nothing
else, in batches, per organization, idempotently, with failure isolation.

Skipped unless EKIP_TEST_* point at a LOCAL migrated database."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.retention import service as retention
from app.shared.config.settings import get_settings
from tests.core.auth.test_password_reset_postgres import _org, _user, env, pw  # noqa: F401  (fixtures + helpers)

_APP_URL = os.environ.get("EKIP_TEST_APP_URL", "")
_SUPER_URL = os.environ.get("EKIP_TEST_POSTGRES_URL", "")


def _local(url: str) -> bool:
    return (urlsplit(url.replace("+asyncpg", "")).hostname or "") in {"localhost", "127.0.0.1", "::1"}


pytestmark = pytest.mark.skipif(
    not (_APP_URL and _SUPER_URL and _local(_APP_URL) and _local(_SUPER_URL)),
    reason="needs a LOCAL migrated database",
)

NOW = datetime.now(UTC)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@pytest_asyncio.fixture
async def rt(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM pg_proc WHERE proname='purge_mcp_requests_batch'"):
        pytest.skip("run `alembic upgrade head` on this database")
    engine = create_async_engine(_APP_URL.replace("?sslmode=disable", ""), poolclass=NullPool, connect_args={"ssl": False})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    pw.scope = scope
    yield pw
    s = f"rm-{pw.tag}-%"
    for sql in (
        "DELETE FROM ingestion_jobs WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM connector_configs WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM scim_pending_actions WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM scim_identities WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM refresh_tokens WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM mcp_requests WHERE tool_name LIKE :t",
        "DELETE FROM email_verifications WHERE email LIKE :e",
        "DELETE FROM password_resets WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)",
    ):
        await pw.sup(sql, s=s, e=f"rm-{pw.tag}-%", t=f"rt-{pw.tag}-%")
    await engine.dispose()


def _settings(**overrides):
    base = {
        "retention_enabled": True, "retention_dry_run": False, "retention_batch_size": 10,
        "retention_batch_pause_seconds": 0.0, "ingestion_job_retention_days": 90,
        "refresh_token_expired_grace_days": 7, "refresh_token_revoked_retention_days": 30,
        "email_verification_retention_days": 7, "password_reset_retention_days": 7,
        "scim_token_retention_days": 90, "scim_pending_action_retention_days": 180,
        "mcp_request_retention_days": 90,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def _nosleep(_):
    return None


async def _run(rt, only=None, **overrides):  # noqa: F811
    return await retention.run_retention(rt.scope, settings=_settings(**overrides), now=NOW, sleep=_nosleep, only=only)


async def _count(rt, table, where, **params):  # noqa: F811
    return (await rt.sup(f"SELECT count(*) FROM {table} WHERE {where}", **params))[0][0]


# ---------------------------------------------------------------- refresh tokens


async def _token(rt, org, user, *, expires_in_days, revoked_days_ago=None, tag="t"):  # noqa: F811
    await rt.sup(
        "INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at, revoked_at) "
        "VALUES (:u,:o,:f,:h,:e,:r)",
        u=user, o=org, f=uuid.uuid4(), h=f"{tag}-{uuid.uuid4().hex}", e=NOW + timedelta(days=expires_in_days),
        r=_ago(revoked_days_ago) if revoked_days_ago is not None else None,
    )


@pytest.mark.asyncio
async def test_only_expired_past_grace_and_revoked_past_retention_refresh_tokens_are_deleted(rt) -> None:  # noqa: F811
    a, b = await _org(rt, "rta"), await _org(rt, "rtb")
    ua, ub = await _user(rt, "rtua", a), await _user(rt, "rtub", b)
    await _token(rt, a, ua, expires_in_days=10)                          # active                      -> kept
    await _token(rt, a, ua, expires_in_days=-2)                          # expired 2d (inside grace)   -> kept
    await _token(rt, a, ua, expires_in_days=-30)                         # expired 30d                 -> deleted
    await _token(rt, a, ua, expires_in_days=10, revoked_days_ago=5)      # revoked 5d                  -> kept
    await _token(rt, a, ua, expires_in_days=10, revoked_days_ago=40)     # revoked 40d                 -> deleted
    await _token(rt, b, ub, expires_in_days=-30)                         # other organization          -> deleted too
    report = await _run(rt, only={"refresh_tokens"})
    assert not report.errors
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=a) == 3
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=b) == 0
    assert await _count(rt, "refresh_tokens", "organization_id=:o AND expires_at > now() AND revoked_at IS NULL", o=a) == 1  # the active one survived
    again = await _run(rt, only={"refresh_tokens"})  # idempotent
    assert again.total_deleted == 0


@pytest.mark.asyncio
async def test_deletion_is_batched(rt) -> None:  # noqa: F811
    org = await _org(rt, "rtbatch")
    user = await _user(rt, "rtbatchu", org)
    for _ in range(35):
        await _token(rt, org, user, expires_in_days=-60)
    await _token(rt, org, user, expires_in_days=5)
    batches = []
    original = retention._delete_batch

    async def spy(session, spec, params, batch_size):
        n = await original(session, spec, params, batch_size)
        if spec.name == "refresh_tokens":
            batches.append(n)
        return n

    retention._delete_batch = spy
    try:
        report = await _run(rt, only={"refresh_tokens"}, retention_batch_size=10)
    finally:
        retention._delete_batch = original
    assert report.total_deleted >= 35
    assert 10 in batches and max(batches) <= 10  # never more than one batch's worth per transaction
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=org) == 1


# ---------------------------------------------------------------- global tables


@pytest.mark.asyncio
async def test_expired_verifications_and_resets_go_and_fresh_ones_stay(rt) -> None:  # noqa: F811
    for name, expires, consumed in (("old", _ago(30), None), ("consumed", NOW + timedelta(days=1), _ago(30)),
                                    ("fresh", NOW + timedelta(days=1), None), ("justexpired", _ago(1), None)):
        await rt.sup(
            "INSERT INTO email_verifications (kind, email, token_hash, expires_at, consumed_at, display_name, password_hash, "
            "organization_name, organization_slug) VALUES ('registration',:e,:h,:x,:c,'N','h','O',:slug)",
            e=rt.email(f"ev{name}"), h=uuid.uuid4().hex, x=expires, c=consumed, slug=f"rm-{rt.tag}-ev{name}",
        )
        uid = await _user(rt, f"pr{name}")
        await rt.sup(
            "INSERT INTO password_resets (user_id, token_hash, expires_at, consumed_at) VALUES (:u,:h,:x,:c)",
            u=uid, h=uuid.uuid4().hex, x=expires, c=consumed,
        )
    report = await _run(rt, only={"email_verifications", "password_resets"})
    assert not report.errors
    left = {r[0] for r in await rt.sup("SELECT email FROM email_verifications WHERE email LIKE :e", e=f"rm-{rt.tag}-%")}
    assert left == {rt.email("evfresh"), rt.email("evjustexpired")}  # expired only 1 day ago is still inside the 7-day period
    assert await _count(rt, "password_resets", "user_id IN (SELECT id FROM users WHERE email LIKE :e)", e=f"rm-{rt.tag}-%") == 2


# ---------------------------------------------------------------- ingestion jobs


async def _connector(rt, org):  # noqa: F811
    cid = uuid.uuid4()
    await rt.sup(
        "INSERT INTO connector_configs (id, organization_id, source, credential_ref, config, status) "
        "VALUES (:i,:o,'github','x','{}'::jsonb,'active')", i=cid, o=org,
    )
    return cid


async def _job(rt, org, cid, *, status, age_days):  # noqa: F811
    jid = uuid.uuid4()
    await rt.sup(
        "INSERT INTO ingestion_jobs (id, organization_id, connector_config_id, status, documents_processed, completed_at, created_at) "
        "VALUES (:i,:o,:c,:s,0,:t,:t)", i=jid, o=org, c=cid, s=status, t=_ago(age_days),
    )
    return jid


@pytest.mark.asyncio
async def test_old_finished_jobs_go_but_active_jobs_and_each_connectors_newest_job_stay(rt) -> None:  # noqa: F811
    org = await _org(rt, "rtjobs")
    c1, c2 = await _connector(rt, org), await _connector(rt, org)
    old_ok = await _job(rt, org, c1, status="succeeded", age_days=200)
    old_failed = await _job(rt, org, c1, status="failed", age_days=150)
    recent = await _job(rt, org, c1, status="succeeded", age_days=10)          # inside the period
    old_running = await _job(rt, org, c1, status="running", age_days=300)      # never deleted
    old_queued = await _job(rt, org, c1, status="queued", age_days=300)        # never deleted
    only_old = await _job(rt, org, c2, status="succeeded", age_days=400)       # c2's NEWEST job: kept
    await _run(rt, only={"ingestion_jobs"})
    left = {r[0] for r in await rt.sup("SELECT id FROM ingestion_jobs WHERE organization_id=:o", o=org)}
    assert left == {recent, old_running, old_queued, only_old}
    assert old_ok not in left and old_failed not in left


# ---------------------------------------------------------------- scim, mcp, dry run, isolation


@pytest.mark.asyncio
async def test_scim_leftovers_and_mcp_requests(rt) -> None:  # noqa: F811
    org = await _org(rt, "rtscim")
    user = await _user(rt, "rtscimu", org)
    for name, exp, rev in (("live", NOW + timedelta(days=30), None), ("expiredold", _ago(120), None),
                           ("revokedold", NOW + timedelta(days=30), _ago(120)), ("revokedrecent", NOW + timedelta(days=30), _ago(5))):
        await rt.sup(
            "INSERT INTO scim_tokens (organization_id, name, token_hash, token_prefix, expires_at, revoked_at) VALUES (:o,:n,:h,'scim_x',:e,:r)",
            o=org, n=name, h=uuid.uuid4().hex, e=exp, r=rev,
        )
    ident = uuid.uuid4()
    await rt.sup("INSERT INTO scim_identities (id, organization_id, user_name, user_name_key) VALUES (:i,:o,'a','a')", i=ident, o=org)
    for status, decided in (("pending", None), ("approved", _ago(300)), ("rejected", _ago(10))):
        sid = uuid.uuid4()
        await rt.sup("INSERT INTO scim_identities (id, organization_id, user_name, user_name_key) VALUES (:i,:o,:n,:n)", i=sid, o=org, n=f"x{status}")
        await rt.sup(
            "INSERT INTO scim_pending_actions (organization_id, scim_identity_id, user_id, reason, status, decided_at) VALUES (:o,:i,:u,'r',:s,:d)",
            o=org, i=sid, u=user, s=status, d=decided,
        )
    for name, age, with_org in (("old", 200, None), ("oldorg", 200, org), ("new", 5, None)):
        await rt.sup(
            "INSERT INTO mcp_requests (tool_name, identity, occurred_at, organization_id) VALUES (:t,'user:x',:d,:o)",
            t=f"rt-{rt.tag}-{name}", d=_ago(age), o=with_org,
        )
    report = await _run(rt, only={"scim_tokens", "scim_pending_actions", "mcp_requests"})
    assert not report.errors
    assert {r[0] for r in await rt.sup("SELECT name FROM scim_tokens WHERE organization_id=:o", o=org)} == {"live", "revokedrecent"}
    assert {r[0] for r in await rt.sup("SELECT status FROM scim_pending_actions WHERE organization_id=:o", o=org)} == {"pending", "rejected"}
    assert {r[0] for r in await rt.sup("SELECT tool_name FROM mcp_requests WHERE tool_name LIKE :t", t=f"rt-{rt.tag}-%")} == {f"rt-{rt.tag}-new"}


@pytest.mark.asyncio
async def test_dry_run_counts_and_deletes_nothing(rt) -> None:  # noqa: F811
    org = await _org(rt, "rtdry")
    user = await _user(rt, "rtdryu", org)
    for _ in range(4):
        await _token(rt, org, user, expires_in_days=-60)
    report = await _run(rt, only={"refresh_tokens"}, retention_dry_run=True)
    assert report.dry_run and report.total_deleted == 0
    assert sum(r.would_delete for r in report.results) >= 4
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=org) == 4


@pytest.mark.asyncio
async def test_one_failing_organization_or_task_does_not_stop_the_rest(rt, monkeypatch) -> None:  # noqa: F811
    a, b = await _org(rt, "rtfa"), await _org(rt, "rtfb")
    ua, ub = await _user(rt, "rtfua", a), await _user(rt, "rtfub", b)
    await _token(rt, a, ua, expires_in_days=-60)
    await _token(rt, b, ub, expires_in_days=-60)
    original = retention._delete_batch
    boom = {"hit": False}

    async def flaky(session, spec, params, batch_size):
        if spec.name == "refresh_tokens" and params.get("org") == a:
            boom["hit"] = True
            raise RuntimeError("simulated failure")
        if spec.name == "password_resets":
            raise RuntimeError("simulated failure")
        return await original(session, spec, params, batch_size)

    monkeypatch.setattr(retention, "_delete_batch", flaky)
    report = await _run(rt, only={"refresh_tokens", "password_resets", "email_verifications"})
    assert boom["hit"] and len(report.errors) >= 2  # both failures are reported, not raised
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=a) == 1  # a's failed batch rolled back; nothing half-deleted
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=b) == 0  # b was still cleaned
    monkeypatch.setattr(retention, "_delete_batch", original)
    assert (await _run(rt, only={"refresh_tokens"})).total_deleted == 1  # the retry converges


@pytest.mark.asyncio
async def test_tenant_scoped_rules_run_under_the_tenants_own_rls_context(rt) -> None:  # noqa: F811
    """A tenant-scoped rule can never touch another organization's rows: with a context for A, B's rows are
    invisible to the DELETE even though the predicate (age) matches them."""
    a, b = await _org(rt, "rtisoa"), await _org(rt, "rtisob")
    ub = await _user(rt, "rtisoub", b)
    await _token(rt, b, ub, expires_in_days=-60)
    spec = next(s for s in retention._specs() if s.name == "refresh_tokens")
    params = {"cutoff": _ago(7), "cutoff2": _ago(30), "org": b, "batch": 100}

    async def attempt(session):
        from sqlalchemy import text

        from app.database.session import set_tenant_context

        await set_tenant_context(session, a)  # context is A, predicate names B
        return await retention._delete_batch(session, spec, {k: v for k, v in params.items() if k != "batch"}, 100)

    async with rt.scope() as session:
        assert await attempt(session) == 0
    assert await _count(rt, "refresh_tokens", "organization_id=:o", o=b) == 1
