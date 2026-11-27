"""Phase 2: a suspended organization is not processed -- against REAL PostgreSQL as `ekip_app`.

Drives the real `_execute_ingestion_job` page loop, the real arq task functions, the real cron
fan-outs and the real cursor/checkpoint persistence. Only three things are replaced: the connector
(a paged fake that records every fetch), item processing (a recorder: no embeddings, no LLM), and
the arq/Redis handle (a recorder). Nothing leaves the machine.

Status is flipped with a direct superuser UPDATE (the lifecycle APIs arrive in a later phase), and
`onboarding` is simply the default every `create_organization` organization starts in.

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL migrated database.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from arq.worker import Retry
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.workers import tasks as agent_tasks
from app.core.exceptions import OrganizationSuspendedError
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.ingestion import service as ingestion_service
from app.ingestion.schemas import FetchResult
from app.ingestion.workers import tasks as ingestion_tasks
from app.shared.security import encrypt_secret, get_kms
from tests.core.users.test_role_management_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _engine,
    _is_local,
    _make_org,
    env,
)

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

SOURCE = "notion"  # a valid ConnectorSource; the registry entry is replaced by the fake for the test only


class PagedConnector:
    """A connector with three pages of two items each. Records every fetch (the cursor it was
    asked for) and, if `on_fetch` is set, runs it when a given page is requested."""

    source_name = SOURCE
    requests_per_second = 1000.0
    supports_resume_token = False

    def __init__(self, pages: int = 3, per_page: int = 2) -> None:
        self.pages, self.per_page = pages, per_page
        self.authenticated = 0
        self.fetch_cursors: list[str | None] = []
        self.on_authenticate = None
        self.on_fetch = None

    async def authenticate(self, config):
        self.authenticated += 1
        if self.on_authenticate is not None:
            await self.on_authenticate()
        return object()

    async def fetch_batch(self, client, *, since, cursor):
        index = 0 if cursor is None else int(cursor[1:])  # cursors are "c1", "c2", ...
        self.fetch_cursors.append(cursor)
        if self.on_fetch is not None:
            await self.on_fetch(index)
        items = [f"item-{index}-{n}" for n in range(self.per_page)]
        last = index >= self.pages - 1
        return FetchResult(items=items, next_cursor=None if last else f"c{index + 1}", has_more=not last)

    def normalize(self, raw_item):  # pragma: no cover - item processing is replaced
        raise AssertionError("not called")

    async def close(self, client) -> None:
        return None


class NoLimit:
    async def acquire(self, key, rate):
        return None


class RecordingRedis:
    def __init__(self):
        self.enqueued: list[tuple[str, str]] = []

    async def enqueue_job(self, name, arg):
        self.enqueued.append((name, arg))


@pytest_asyncio.fixture
async def ing(env, monkeypatch):  # noqa: F811
    migrated = await env.sup(
        "SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='sessions_valid_after'"
    )
    if not migrated:
        pytest.skip("run `alembic upgrade head` on this database first")

    app_engine = _engine(_APP_URL)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    # worker code opens its own sessions with the global `session_scope`; point every one of those at
    # this test's own engine (same role, same RLS) so nothing pools connections across event loops
    for module in (ingestion_tasks, ingestion_service, agent_tasks):
        monkeypatch.setattr(module, "session_scope", scope)

    connector = PagedConnector()
    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, SOURCE, connector)
    processed: list[str] = []
    decrypts = {"n": 0}

    async def record_item(session, *, raw_item, **kwargs):
        processed.append(raw_item)
        return ingestion_service._ItemProcessingResult(1, 0)

    real_decrypt = ingestion_service.decrypt_secret

    async def counting_decrypt(kms, blob):
        decrypts["n"] += 1
        return await real_decrypt(kms, blob)

    monkeypatch.setattr(ingestion_service, "_process_one_item", record_item)
    monkeypatch.setattr(ingestion_service, "decrypt_secret", counting_decrypt)

    env.scope, env.connector, env.processed, env.decrypts = scope, connector, processed, decrypts
    created_orgs: list[uuid.UUID] = []

    async def make_org(name):
        org = await _make_org(env, name)
        created_orgs.append(org)
        return org

    async def make_connector(org, *, status="active", config=None):
        async def go(s):
            project = await tenancy_repository.get_default_project(s, org)
            row = await tenancy_repository.insert_connector_config(
                s,
                organization_id=org,
                source=SOURCE,
                credential_ref=await encrypt_secret(get_kms(), "the-connector-credential"),
                project_id=project.id,
                config=config or {},
                status=status,
            )
            return row.id

        return await env.run(org, go)

    env.make_org, env.make_connector = make_org, make_connector
    yield env

    ids = ", ".join(f"'{o}'" for o in created_orgs) or "NULL"
    await env.sup(f"DELETE FROM ingestion_jobs WHERE organization_id IN ({ids})")
    await env.sup(f"DELETE FROM documents WHERE organization_id IN ({ids})")
    await env.sup(f"DELETE FROM connector_configs WHERE organization_id IN ({ids})")
    await app_engine.dispose()


async def _set_status(env, org, status):
    await env.sup("UPDATE organizations SET status=:s WHERE id=:o", s=status, o=org)


async def _run(env, connector_id, attempt=1):
    async with env.scope() as session:
        return await ingestion_service.run_ingestion_job(
            session, connector_id, rate_limiter=NoLimit(), attempt_number=attempt
        )


async def _jobs(env, connector_id):
    return await env.sup(
        "SELECT status, failed_stage, pages_fetched, documents_processed, last_error_type, completed_at IS NOT NULL "
        "FROM ingestion_jobs WHERE connector_config_id=:c ORDER BY created_at",
        c=connector_id,
    )


async def _connector_row(env, connector_id):
    return (await env.sup("SELECT status, last_synced_at, config FROM connector_configs WHERE id=:c", c=connector_id))[0]


# ============================================================ before anything is touched


@pytest.mark.asyncio
async def test_a_suspended_organization_is_refused_before_the_credential_is_decrypted(ing) -> None:
    org = await ing.make_org("s1")
    connector_id = await ing.make_connector(org)
    await _set_status(ing, org, "suspended")

    with pytest.raises(OrganizationSuspendedError):
        await _run(ing, connector_id)

    assert ing.decrypts["n"] == 0  # the credential was never decrypted
    assert ing.connector.authenticated == 0 and ing.connector.fetch_cursors == []  # the provider was never contacted
    assert ing.processed == []
    assert await _jobs(ing, connector_id) == []  # not even a job row
    assert (await _connector_row(ing, connector_id))[0] == "active"  # connector state untouched


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, ingestion_tasks.MAX_JOB_TRIES])
async def test_a_job_queued_before_the_suspension_is_skipped_when_a_worker_picks_it_up(ing, attempt) -> None:
    org = await ing.make_org("q1")
    connector_id = await ing.make_connector(org)
    # the job sat in the queue; the organization was suspended before a worker started it
    await _set_status(ing, org, "suspended")

    result = await ingestion_tasks.run_ingestion_job_task({"job_try": attempt}, str(connector_id))

    assert result is None  # a clean finish: no Retry, no exception, nothing for arq to re-run
    assert ing.decrypts["n"] == 0 and ing.processed == [] and ing.connector.authenticated == 0
    assert await _jobs(ing, connector_id) == []


# ============================================================ normal organizations keep working


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["onboarding", "active"])
async def test_onboarding_and_active_organizations_are_processed_normally(ing, status) -> None:
    org = await ing.make_org(f"ok-{status}")
    if status == "active":
        await _set_status(ing, org, "active")
    connector_id = await ing.make_connector(org)

    job = await _run(ing, connector_id)

    assert job.status == "succeeded" and job.pages_fetched == 3 and job.documents_processed == 6
    assert len(ing.processed) == 6 and ing.decrypts["n"] == 1
    status_, synced, config = await _connector_row(ing, connector_id)
    assert status_ == "active" and synced is not None
    assert ingestion_service._CHECKPOINT_CONFIG_KEY in config and config[ingestion_service._CHECKPOINT_CONFIG_KEY] is None  # cleared on success


@pytest.mark.asyncio
async def test_other_organizations_keep_processing_while_one_is_suspended(ing) -> None:
    a, b = await ing.make_org("oa"), await ing.make_org("ob")
    conn_a, conn_b = await ing.make_connector(a), await ing.make_connector(b)
    await _set_status(ing, a, "suspended")

    assert await ingestion_tasks.run_ingestion_job_task({"job_try": 1}, str(conn_a)) is None
    assert await ingestion_tasks.run_ingestion_job_task({"job_try": 1}, str(conn_b)) is None

    assert await _jobs(ing, conn_a) == []
    ((status, *_),) = await _jobs(ing, conn_b)
    assert status == "succeeded"  # B was processed, in full, right after A was skipped


# ============================================================ stopping a running job


@pytest.mark.asyncio
async def test_a_running_job_stops_at_the_next_page_boundary_and_keeps_its_cursor(ing) -> None:
    org = await ing.make_org("run")
    connector_id = await ing.make_connector(org)

    async def suspend_while_fetching_page_two(index: int) -> None:
        if index == 1:  # the second page is being fetched when the suspension lands
            await _set_status(ing, org, "suspended")

    ing.connector.on_fetch = suspend_while_fetching_page_two

    job = await _run(ing, connector_id, attempt=ingestion_tasks.MAX_JOB_TRIES)

    # pages 1 and 2 were completed and made durable; page 3 was NEVER requested
    assert ing.connector.fetch_cursors == [None, "c1"]
    assert ing.processed == ["item-0-0", "item-0-1", "item-1-0", "item-1-1"]  # no data processed after the stop
    assert job.status == "failed" and job.failed_stage == ingestion_service.ORGANIZATION_SUSPENDED_STAGE
    assert job.pages_fetched == 2 and job.documents_processed == 4  # honest counts of what DID run
    ((status, stage, pages, docs, error_type, completed),) = await _jobs(ing, connector_id)
    assert (status, stage, pages, docs, error_type, completed) == (
        "failed", "organization_suspended", 2, 4, "OrganizationSuspendedError", True
    )  # closed, never left "running", never reported as a success

    # connector state and cursor preserved: still "active" (NOT "error"), not marked synced, checkpoint -> page 3
    status_, synced, config = await _connector_row(ing, connector_id)
    assert status_ == "active" and synced is None
    assert config[ingestion_service._CHECKPOINT_CONFIG_KEY]["cursor"] == "c2"


@pytest.mark.asyncio
async def test_the_task_treats_a_stopped_job_as_final_even_on_the_last_attempt(ing) -> None:
    org = await ing.make_org("task")
    connector_id = await ing.make_connector(org)

    async def suspend(index: int) -> None:
        if index == 0:
            await _set_status(ing, org, "suspended")

    ing.connector.on_fetch = suspend

    for attempt in (1, ingestion_tasks.MAX_JOB_TRIES):  # neither a retry nor the dead-letter queue
        assert await ingestion_tasks.run_ingestion_job_task({"job_try": attempt}, str(connector_id)) is None
        break
    statuses = [row[0] for row in await _jobs(ing, connector_id)]
    assert statuses == ["failed"] and "dead_lettered" not in statuses


@pytest.mark.asyncio
async def test_suspension_between_the_start_check_and_the_first_fetch_fetches_nothing(ing) -> None:
    org = await ing.make_org("early")
    connector_id = await ing.make_connector(org)

    async def suspend_during_authentication():
        await _set_status(ing, org, "suspended")

    ing.connector.on_authenticate = suspend_during_authentication

    job = await _run(ing, connector_id)

    assert ing.connector.authenticated == 1  # the race window: it had already started ...
    assert ing.connector.fetch_cursors == [] and ing.processed == []  # ... but no tenant page was ever fetched
    assert job.failed_stage == "organization_suspended" and job.pages_fetched == 0
    assert (await _connector_row(ing, connector_id))[0] == "active"


# ============================================================ reactivation resumes, without duplicates


@pytest.mark.asyncio
async def test_a_reactivated_organization_resumes_from_its_cursor_without_reprocessing(ing) -> None:
    org = await ing.make_org("resume")
    connector_id = await ing.make_connector(org)

    async def suspend_on_page_two(index: int) -> None:
        if index == 1:
            await _set_status(ing, org, "suspended")

    ing.connector.on_fetch = suspend_on_page_two
    await _run(ing, connector_id)
    first_run_items = list(ing.processed)
    assert first_run_items == ["item-0-0", "item-0-1", "item-1-0", "item-1-1"]

    # no manual queue/state cleanup: the organization is simply usable again
    ing.connector.on_fetch = None
    ing.connector.fetch_cursors.clear()
    await _set_status(ing, org, "active")
    job = await _run(ing, connector_id)

    assert ing.connector.fetch_cursors == ["c2"]  # resumed at the saved cursor -- pages 1 and 2 were NOT re-fetched
    assert ing.processed == first_run_items + ["item-2-0", "item-2-1"]  # nothing processed twice
    assert job.status == "succeeded"
    status_, synced, config = await _connector_row(ing, connector_id)
    assert status_ == "active" and synced is not None and config[ingestion_service._CHECKPOINT_CONFIG_KEY] is None
    assert [r[0] for r in await _jobs(ing, connector_id)] == ["failed", "succeeded"]  # the history shows both


# ============================================================ scheduled reconciliation


@pytest.mark.asyncio
async def test_reconciliation_excludes_suspended_organizations_and_resumes_after_reactivation(ing) -> None:
    active, onboarding, suspended = await ing.make_org("ra"), await ing.make_org("rb"), await ing.make_org("rc")
    await _set_status(ing, active, "active")
    c_active, c_onb = await ing.make_connector(active), await ing.make_connector(onboarding)
    c_errored = await ing.make_connector(active, status="error")  # failed last time: still retried
    c_connecting = await ing.make_connector(onboarding, status="connecting")  # unchanged rule: not yet connected
    c_disconnected = await ing.make_connector(active, status="disconnected")  # unchanged rule: turned off
    c_susp = await ing.make_connector(suspended)
    await _set_status(ing, suspended, "suspended")
    mine = {c_active, c_onb, c_errored, c_connecting, c_disconnected, c_susp}

    async with ing.scope() as session:
        eligible, skipped = await ingestion_service.list_reconciliation_connector_config_ids(session)
    assert mine & set(eligible) == {c_active, c_onb, c_errored}
    assert skipped >= 1  # the suspended organization's connector was counted as skipped (others' leftovers may add)

    # through the real cron task: only eligible connectors are enqueued
    redis = RecordingRedis()
    await ingestion_tasks.scheduled_reconciliation({"redis": redis})
    enqueued = {arg for name, arg in redis.enqueued if name == "run_ingestion_job_task"}
    assert {str(c) for c in mine} & enqueued == {str(c_active), str(c_onb), str(c_errored)}

    # reactivation: the same connector is simply eligible again on the next tick -- nothing was disabled
    await _set_status(ing, suspended, "active")
    redis = RecordingRedis()
    await ingestion_tasks.scheduled_reconciliation({"redis": redis})
    assert str(c_susp) in {arg for _name, arg in redis.enqueued}
    assert (await _connector_row(ing, c_susp))[0] == "active"


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_if_the_status_filter_breaks(ing, monkeypatch) -> None:
    org = await ing.make_org("fc")
    await ing.make_connector(org)

    async def broken(session, ids):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(ingestion_service.tenancy_service, "get_processable_organization_ids", broken)
    redis = RecordingRedis()
    with pytest.raises(RuntimeError):
        await ingestion_tasks.scheduled_reconciliation({"redis": redis})
    assert redis.enqueued == []


# ============================================================ agent fan-outs and tasks


@pytest.mark.asyncio
async def test_agent_crons_exclude_suspended_organizations_but_keep_everyone_else(ing) -> None:
    onboarding, active, suspended = await ing.make_org("ga"), await ing.make_org("gb"), await ing.make_org("gc")
    await _set_status(ing, active, "active")
    await _set_status(ing, suspended, "suspended")

    for cron, job in (
        (agent_tasks.scheduled_knowledge_gap_scan, "run_knowledge_gap_detection_task"),
        (agent_tasks.scheduled_pattern_detection_scan, "run_pattern_detection_task"),
    ):
        redis = RecordingRedis()
        await cron({"redis": redis})
        queued = {arg for name, arg in redis.enqueued if name == job}
        assert str(onboarding) in queued and str(active) in queued
        assert str(suspended) not in queued

    async with ing.scope() as session:
        ids = {o.id for o in await tenancy_service.list_processable_organizations(session)}
    assert {onboarding, active} <= ids and suspended not in ids


@pytest.mark.asyncio
async def test_pattern_detection_task_runs_for_usable_organizations_and_skips_a_suspended_one(ing, monkeypatch) -> None:
    usable, suspended = await ing.make_org("pa"), await ing.make_org("pb")
    await _set_status(ing, suspended, "suspended")

    ran: list[str] = []
    real = agent_tasks.proactive_service._run_one_detector

    async def spy(session, *, organization_id, finding_type):
        ran.append(f"{organization_id}:{finding_type}")
        return await real(session, organization_id=organization_id, finding_type=finding_type)

    monkeypatch.setattr(agent_tasks.proactive_service, "_run_one_detector", spy)

    assert await agent_tasks.run_pattern_detection_task({}, str(suspended)) is None  # `ctx` has no job_try: no Retry path
    assert ran == []  # no detector read the suspended organization
    await agent_tasks.run_pattern_detection_task({"job_try": 1}, str(usable))  # the real detectors, real database
    assert ran and all(entry.startswith(str(usable)) for entry in ran)


@pytest.mark.asyncio
async def test_a_detection_run_stops_between_detectors_when_the_organization_is_suspended(ing, monkeypatch) -> None:
    org = await ing.make_org("pm")
    ran: list[str] = []

    async def fake_one(session, *, organization_id, finding_type):
        ran.append(finding_type)
        if len(ran) == 1:
            await _set_status(ing, org, "suspended")  # lands while the first detector runs
        return SimpleNamespace(  # only the attributes the task logs
            finding_type=finding_type, candidate_count=0, created_count=0, deactivated_count=0, error=None
        )

    monkeypatch.setattr(agent_tasks.proactive_service, "_run_one_detector", fake_one)

    assert await agent_tasks.run_pattern_detection_task({"job_try": 1}, str(org)) is None  # clean end, no Retry
    assert len(ran) == 1  # the second detector never started


@pytest.mark.asyncio
async def test_knowledge_gap_task_skips_a_suspended_organization_and_stops_mid_run(ing, monkeypatch) -> None:
    usable, suspended = await ing.make_org("ka"), await ing.make_org("kb")
    await _set_status(ing, suspended, "suspended")
    runs: list[uuid.UUID] = []
    boundaries = {"checked_ok": 0, "stopped": 0}

    async def fake_detect(session, actor, *, boundary_check=None, **kw):
        runs.append(actor.organization_id)
        await boundary_check()  # boundary 1: usable
        boundaries["checked_ok"] += 1
        await _set_status(ing, usable, "suspended")  # suspended mid-run (e.g. during an LLM call)
        try:
            await boundary_check()  # boundary 2
        finally:
            boundaries["stopped"] += 1
        raise AssertionError("must not continue after the boundary")  # pragma: no cover

    monkeypatch.setattr(agent_tasks.agents_service, "detect_knowledge_gaps", fake_detect)

    assert await agent_tasks.run_knowledge_gap_detection_task({}, str(suspended)) is None
    assert runs == []  # the agent (and its LLM calls) never started for the suspended organization
    assert await agent_tasks.run_knowledge_gap_detection_task({"job_try": 1}, str(usable)) is None  # stopped, not retried
    assert runs == [usable] and boundaries == {"checked_ok": 1, "stopped": 1}


@pytest.mark.asyncio
async def test_the_agent_gate_reads_fresh_status_not_a_cached_row(ing) -> None:
    """Within ONE session the status is re-read from the database on every call -- a stale ORM
    object must never let a long run miss a suspension that another session committed."""
    org = await ing.make_org("fresh")
    async with ing.scope() as session:
        await tenancy_service.assert_organization_processing_allowed(session, org)  # fine now
        await _set_status(ing, org, "suspended")  # committed by a different connection
        with pytest.raises(OrganizationSuspendedError):
            await tenancy_service.assert_organization_processing_allowed(session, org)
        await _set_status(ing, org, "active")
        await tenancy_service.assert_organization_processing_allowed(session, org)  # and back
