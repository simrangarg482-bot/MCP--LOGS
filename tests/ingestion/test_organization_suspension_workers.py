"""Phase 2 of the organization lifecycle -- background processing -- WITHOUT a database.

The gate itself, how the arq task functions react to it (a clean, NON-retriable outcome; a failed
job still retries; an exhausted one is still dead-lettered), the cron fan-outs and the
invitation-email delivery task. What needs a real database (the page-boundary stop, the cursor,
reconciliation against `connector_configs`) is in
`test_organization_suspension_workers_postgres.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from arq.worker import Retry

from app.agents.workers import tasks as agent_tasks
from app.core.exceptions import NotFoundError, OrganizationSuspendedError
from app.core.tenancy import invitation_email
from app.core.tenancy import service as tenancy_service
from app.ingestion import service as ingestion_service
from app.ingestion.workers import tasks as ingestion_tasks

ORG = uuid.uuid4()


class _Session:
    pass


@asynccontextmanager
async def _scope():
    yield _Session()


# ------------------------------------------------------------------ the gate itself


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["onboarding", "active", "ACTIVE", " onboarding "])
async def test_onboarding_and_active_organizations_pass_the_gate(monkeypatch, status) -> None:
    async def fake_status(session, organization_id):
        return True, status

    monkeypatch.setattr(tenancy_service.repository, "get_organization_status", fake_status)
    await tenancy_service.assert_organization_processing_allowed(_Session(), ORG)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["suspended", "Suspended", "  SUSPENDED "])
async def test_a_suspended_organization_fails_the_gate_with_the_non_retriable_type(monkeypatch, status) -> None:
    async def fake_status(session, organization_id):
        return True, status

    monkeypatch.setattr(tenancy_service.repository, "get_organization_status", fake_status)
    with pytest.raises(OrganizationSuspendedError) as info:
        await tenancy_service.assert_organization_processing_allowed(_Session(), ORG)
    assert info.value.error_code == "auth.organization_suspended"


@pytest.mark.asyncio
async def test_a_missing_organization_is_not_found_and_an_unknown_status_is_allowed_but_logged(monkeypatch) -> None:
    state = {"row": (False, None)}

    async def fake_status(session, organization_id):
        return state["row"]

    warnings: list[str] = []
    monkeypatch.setattr(tenancy_service.repository, "get_organization_status", fake_status)
    monkeypatch.setattr(tenancy_service.logger, "warning", lambda event, **kw: warnings.append(event))
    with pytest.raises(NotFoundError):
        await tenancy_service.assert_organization_processing_allowed(_Session(), ORG)
    state["row"] = (True, "trial")
    await tenancy_service.assert_organization_processing_allowed(_Session(), ORG)
    assert warnings == ["organization_status_unrecognized"]


@pytest.mark.asyncio
async def test_list_processable_organizations_drops_only_the_suspended(monkeypatch) -> None:
    rows = [
        SimpleNamespace(id=uuid.uuid4(), status="onboarding"),
        SimpleNamespace(id=uuid.uuid4(), status="active"),
        SimpleNamespace(id=uuid.uuid4(), status="suspended"),
        SimpleNamespace(id=uuid.uuid4(), status="Suspended "),
    ]

    async def fake_list(session):
        return rows

    monkeypatch.setattr(tenancy_service, "list_organizations", fake_list)
    kept = await tenancy_service.list_processable_organizations(_Session())
    assert [o.id for o in kept] == [rows[0].id, rows[1].id]


@pytest.mark.asyncio
async def test_get_processable_organization_ids_is_one_query_and_drops_suspended_and_unknown(monkeypatch) -> None:
    a, b, c, missing = (uuid.uuid4() for _ in range(4))
    calls = []

    async def fake_statuses(session, ids):
        calls.append(list(ids))
        return {a: "active", b: "suspended", c: "onboarding"}

    monkeypatch.setattr(tenancy_service.repository, "get_organization_statuses", fake_statuses)
    kept = await tenancy_service.get_processable_organization_ids(_Session(), [a, b, c, missing])
    assert kept == {a, c} and len(calls) == 1


# ------------------------------------------------------------------ ingestion task outcomes


def _job(status: str, stage: str | None = None):
    return SimpleNamespace(id=uuid.uuid4(), organization_id=ORG, status=status, failed_stage=stage)


@pytest.fixture
def ingestion_env(monkeypatch):
    state = {"result": None, "dead_lettered": 0}
    monkeypatch.setattr(ingestion_tasks, "session_scope", _scope)

    async def run(session, connector_config_id, **kw):
        if isinstance(state["result"], BaseException):
            raise state["result"]
        return state["result"]

    async def dead_letter(session, job_id, organization_id):
        state["dead_lettered"] += 1
        return _job("dead_lettered")

    monkeypatch.setattr(ingestion_tasks.service, "run_ingestion_job", run)
    monkeypatch.setattr(ingestion_tasks.service, "dead_letter_ingestion_job", dead_letter)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_a_queued_job_for_a_suspended_organization_is_skipped_without_a_retry(ingestion_env, attempt) -> None:
    ingestion_env["result"] = OrganizationSuspendedError("suspended")

    # returns normally on EVERY attempt: arq sees a finished job, not a retry
    assert await ingestion_tasks.run_ingestion_job_task({"job_try": attempt}, str(uuid.uuid4())) is None
    assert ingestion_env["dead_lettered"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, ingestion_tasks.MAX_JOB_TRIES])
async def test_a_job_stopped_for_suspension_is_neither_retried_nor_dead_lettered(ingestion_env, attempt) -> None:
    ingestion_env["result"] = _job("failed", ingestion_service.ORGANIZATION_SUSPENDED_STAGE)

    assert await ingestion_tasks.run_ingestion_job_task({"job_try": attempt}, str(uuid.uuid4())) is None
    assert ingestion_env["dead_lettered"] == 0


@pytest.mark.asyncio
async def test_ordinary_failures_still_retry_and_exhaust_to_the_dead_letter_queue(ingestion_env) -> None:
    ingestion_env["result"] = _job("failed", "fetch")
    with pytest.raises(Retry):
        await ingestion_tasks.run_ingestion_job_task({"job_try": 1}, str(uuid.uuid4()))
    assert ingestion_env["dead_lettered"] == 0

    await ingestion_tasks.run_ingestion_job_task({"job_try": ingestion_tasks.MAX_JOB_TRIES}, str(uuid.uuid4()))
    assert ingestion_env["dead_lettered"] == 1  # unchanged behaviour for a real failure


@pytest.mark.asyncio
async def test_other_errors_still_retry(ingestion_env) -> None:
    ingestion_env["result"] = RuntimeError("boom")
    with pytest.raises(Retry):
        await ingestion_tasks.run_ingestion_job_task({"job_try": 1}, str(uuid.uuid4()))


# ------------------------------------------------------------------ cron fan-outs


class _Redis:
    def __init__(self):
        self.enqueued: list[tuple[str, str]] = []

    async def enqueue_job(self, name, arg):
        self.enqueued.append((name, arg))


@pytest.mark.asyncio
async def test_reconciliation_enqueues_only_what_the_service_declares_eligible(monkeypatch) -> None:
    keep = [uuid.uuid4(), uuid.uuid4()]
    monkeypatch.setattr(ingestion_tasks, "session_scope", _scope)

    async def fake_list(session):
        return keep, 3

    monkeypatch.setattr(ingestion_tasks.service, "list_reconciliation_connector_config_ids", fake_list)
    redis = _Redis()
    await ingestion_tasks.scheduled_reconciliation({"redis": redis})
    assert redis.enqueued == [("run_ingestion_job_task", str(i)) for i in keep]


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_enqueueing_nothing_if_the_filter_cannot_run(monkeypatch) -> None:
    monkeypatch.setattr(ingestion_tasks, "session_scope", _scope)

    async def broken(session):
        raise RuntimeError("status lookup failed")

    monkeypatch.setattr(ingestion_tasks.service, "list_reconciliation_connector_config_ids", broken)
    redis = _Redis()
    with pytest.raises(RuntimeError):
        await ingestion_tasks.scheduled_reconciliation({"redis": redis})
    assert redis.enqueued == []  # never "enqueue everything unfiltered"


@pytest.mark.asyncio
async def test_reconciliation_filter_drops_connectors_of_suspended_and_unknown_organizations(monkeypatch) -> None:
    c_active, c_susp, c_gone, c_onb = (uuid.uuid4() for _ in range(4))
    o_active, o_susp, o_onb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def fake_ids(session):
        return [c_active, c_susp, c_gone, c_onb]

    async def fake_orgs(session, ids):
        return {c_active: o_active, c_susp: o_susp, c_onb: o_onb}  # c_gone: connector vanished

    async def fake_processable(session, org_ids):
        assert set(org_ids) == {o_active, o_susp, o_onb}
        return {o_active, o_onb}

    monkeypatch.setattr(ingestion_service.repository, "list_active_connector_config_ids", fake_ids)
    monkeypatch.setattr(ingestion_service.repository, "resolve_connector_config_organization_ids", fake_orgs)
    monkeypatch.setattr(ingestion_service.tenancy_service, "get_processable_organization_ids", fake_processable)

    eligible, skipped = await ingestion_service.list_reconciliation_connector_config_ids(_Session())

    assert eligible == [c_active, c_onb] and skipped == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cron", "job"),
    [
        (agent_tasks.scheduled_knowledge_gap_scan, "run_knowledge_gap_detection_task"),
        (agent_tasks.scheduled_pattern_detection_scan, "run_pattern_detection_task"),
    ],
)
async def test_agent_crons_enqueue_only_processable_organizations(monkeypatch, cron, job) -> None:
    keep = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    monkeypatch.setattr(agent_tasks, "session_scope", _scope)

    async def fake_processable(session):
        return keep

    async def must_not_be_used(session):  # pragma: no cover
        raise AssertionError("the unfiltered listing must not feed a fan-out")

    monkeypatch.setattr(agent_tasks.tenancy_service, "list_processable_organizations", fake_processable)
    monkeypatch.setattr(agent_tasks.tenancy_service, "list_organizations", must_not_be_used)
    redis = _Redis()

    await cron({"redis": redis})

    assert redis.enqueued == [(job, str(o.id)) for o in keep]


# ------------------------------------------------------------------ agent task outcomes


def _agent_env(monkeypatch, *, suspended_at_start=False):
    calls = {"detect": 0, "detection": 0, "boundary": None}
    monkeypatch.setattr(agent_tasks, "session_scope", _scope)

    async def no_context(session, organization_id):
        return None

    async def gate(session, organization_id):
        if suspended_at_start or calls.get("now_suspended"):
            raise OrganizationSuspendedError("suspended")

    async def detect(session, actor, *, boundary_check=None, **kw):
        calls["detect"] += 1
        calls["boundary"] = boundary_check
        return []

    async def detection(session, *, organization_id, boundary_check=None, **kw):
        calls["detection"] += 1
        calls["boundary"] = boundary_check
        return []

    monkeypatch.setattr(agent_tasks, "set_tenant_context", no_context)
    monkeypatch.setattr(agent_tasks.tenancy_service, "assert_organization_processing_allowed", gate)
    monkeypatch.setattr(agent_tasks.agents_service, "detect_knowledge_gaps", detect)
    monkeypatch.setattr(agent_tasks.proactive_service, "run_detection", detection)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["run_knowledge_gap_detection_task", "run_pattern_detection_task"])
async def test_agent_jobs_for_a_suspended_organization_never_start_and_are_not_retried(monkeypatch, task) -> None:
    calls = _agent_env(monkeypatch, suspended_at_start=True)

    # `ctx` has no "job_try": reaching the generic retry branch would KeyError -- it must not be reached
    assert await getattr(agent_tasks, task)({}, str(ORG)) is None
    assert calls["detect"] == 0 and calls["detection"] == 0


@pytest.mark.asyncio
async def test_agent_jobs_for_a_usable_organization_run_and_receive_a_boundary_check(monkeypatch) -> None:
    calls = _agent_env(monkeypatch)

    await agent_tasks.run_knowledge_gap_detection_task({}, str(ORG))
    await agent_tasks.run_pattern_detection_task({}, str(ORG))

    assert calls["detect"] == 1 and calls["detection"] == 1 and callable(calls["boundary"])


@pytest.mark.asyncio
async def test_a_suspension_during_the_run_stops_it_cleanly_at_the_next_boundary(monkeypatch) -> None:
    calls = _agent_env(monkeypatch)

    async def detection(session, *, organization_id, boundary_check=None, **kw):
        await boundary_check()  # boundary 1: still fine
        calls["now_suspended"] = True  # suspended while a detector was running
        await boundary_check()  # boundary 2: raises -> the run stops
        raise AssertionError("no further detector may run")  # pragma: no cover

    monkeypatch.setattr(agent_tasks.proactive_service, "run_detection", detection)
    assert await agent_tasks.run_pattern_detection_task({}, str(ORG)) is None  # clean end, no Retry


@pytest.mark.asyncio
async def test_other_agent_failures_still_retry(monkeypatch) -> None:
    _agent_env(monkeypatch)

    async def boom(session, actor, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(agent_tasks.agents_service, "detect_knowledge_gaps", boom)
    with pytest.raises(Retry):
        await agent_tasks.run_knowledge_gap_detection_task({"job_try": 1}, str(ORG))


# ------------------------------------------------------------------ boundary hooks in the loops


@pytest.mark.asyncio
async def test_run_detection_checks_the_boundary_before_every_detector_and_keeps_no_partial_results(monkeypatch) -> None:
    from app.core.proactive import service as proactive

    ran: list[str] = []

    async def fake_one(session, *, organization_id, finding_type):
        ran.append(finding_type)
        return SimpleNamespace(finding_type=finding_type)

    monkeypatch.setattr(proactive, "_run_one_detector", fake_one)
    checks = {"n": 0}

    async def check():
        checks["n"] += 1
        if checks["n"] == 3:
            raise OrganizationSuspendedError("suspended")

    with pytest.raises(OrganizationSuspendedError):
        await proactive.run_detection(_Session(), organization_id=ORG, finding_types=["a", "b", "c", "d"], boundary_check=check)
    assert ran == ["a", "b"]  # "c" and "d" never started
    assert (await proactive.run_detection(_Session(), organization_id=ORG, finding_types=["x", "y"]))[0].finding_type == "x"  # no check: unchanged


# ------------------------------------------------------------------ post-request invitation email


@pytest.mark.asyncio
async def test_invitation_email_is_not_sent_for_a_suspended_organization(monkeypatch) -> None:
    sent: list[object] = []
    recorded: list[dict] = []

    class Sender:
        async def send(self, message):
            sent.append(message)

    async def fake_status(session, organization_id):
        return True, "suspended"

    async def fake_record(pending, *, status, attempts, session_factory, now):
        recorded.append({"status": status, "attempts": attempts})

    monkeypatch.setattr(invitation_email.repository, "get_organization_status", fake_status)
    monkeypatch.setattr(invitation_email, "_record_outcome", fake_record)
    pending = SimpleNamespace(organization_id=ORG, invitation_id=uuid.uuid4(), message=object())

    result = await invitation_email.deliver_invitation_email(pending, sender=Sender(), session_factory=_scope)

    assert result == "failed" and sent == [] and recorded == [{"status": "failed", "attempts": 0}]


@pytest.mark.asyncio
async def test_invitation_email_still_goes_out_for_a_usable_organization_and_when_the_lookup_errors(monkeypatch) -> None:
    sent: list[object] = []
    recorded: list[str] = []

    class Sender:
        async def send(self, message):
            sent.append(message)

    async def fake_record(pending, *, status, attempts, session_factory, now):
        recorded.append(status)

    monkeypatch.setattr(invitation_email, "_record_outcome", fake_record)
    pending = SimpleNamespace(organization_id=ORG, invitation_id=uuid.uuid4(), message=object())

    async def active(session, organization_id):
        return True, "onboarding"

    monkeypatch.setattr(invitation_email.repository, "get_organization_status", active)
    assert await invitation_email.deliver_invitation_email(pending, sender=Sender(), session_factory=_scope) == "sent"

    async def broken(session, organization_id):
        raise RuntimeError("db blip")

    monkeypatch.setattr(invitation_email.repository, "get_organization_status", broken)
    assert await invitation_email.deliver_invitation_email(pending, sender=Sender(), session_factory=_scope) == "sent"
    assert len(sent) == 2 and recorded == ["sent", "sent"]  # a lookup error never silently drops a legitimate email
