"""Tests for `app.core.privacy.service` -- data-subject deletion.

`repository.py`'s functions are monkeypatched with in-memory fakes (the same
style as `tests/core/knowledge/test_service.py`), so these exercise the
service's own ownership decisions, sequencing, idempotency, partial-failure
accounting and authorization -- not ORM/SQL behavior. The repository's SQL
scoping is covered separately by `test_repository_scoping.py`.

These are deliberately thorough. Deletion code that misunderstands ownership
destroys data that cannot be recovered, and the whole point of this module is
that the *retain* decisions are as load-bearing as the delete ones.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.privacy import service as privacy_service
from app.shared.schemas import ActorKind, Identity


def _admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )


def _member_no_permissions(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


@pytest.fixture()
def repo(monkeypatch):
    """Installs counting/mutating fakes over every repository function and
    returns a mutable state dict the tests can assert against."""
    state: dict[str, object] = {
        "user_exists": True,
        "is_anonymized": False,
        "email": "person@example.com",
        "counts": {
            "refresh_tokens": 3,
            "user_roles": 1,
            "project_memberships": 2,
            "external_identity_mappings": 1,
            "agent_executions": 40,
            "invitations": 1,
            "incidents_reported": 7,
            "user_private_memory": 4,
        },
        "calls": [],
        "revoked_sessions": 0,
    }

    async def _count(key):
        return state["counts"][key]  # type: ignore[index]

    monkeypatch.setattr(
        privacy_service.repository, "user_exists",
        lambda s, u: _async(state["user_exists"]),
    )
    monkeypatch.setattr(
        privacy_service.repository, "is_user_anonymized",
        lambda s, u: _async(state["is_anonymized"]),
    )
    monkeypatch.setattr(
        privacy_service.repository, "get_user_email",
        lambda s, u: _async(state["email"]),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_refresh_tokens",
        lambda s, u, o: _count("refresh_tokens"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_user_roles",
        lambda s, u, o: _count("user_roles"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_project_memberships",
        lambda s, u, o: _count("project_memberships"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_external_identity_mappings",
        lambda s, u, o: _count("external_identity_mappings"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_agent_executions",
        lambda s, u, o: _count("agent_executions"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_invitations_for_email",
        lambda s, e, o: _count("invitations"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "count_incidents_reported",
        lambda s, u, o: _count("incidents_reported"),
    )
    monkeypatch.setattr(
        privacy_service.repository, "anonymized_email_for",
        lambda u: f"deleted-user-{u}@deleted.invalid",
    )
    # Priority 4 integration: user-private agent memory is hard-deleted as
    # part of the same plan/execution.
    monkeypatch.setattr(
        privacy_service.memory_repository, "count_user_scoped",
        lambda s, u, o: _count("user_private_memory"),
    )

    def _mutation(name, rows):
        async def _fn(*args, **kwargs):
            state["calls"].append(name)  # type: ignore[union-attr]
            return rows
        return _fn

    monkeypatch.setattr(
        privacy_service.repository, "delete_refresh_tokens", _mutation("delete_refresh_tokens", 3)
    )
    monkeypatch.setattr(
        privacy_service.repository, "delete_user_roles", _mutation("delete_user_roles", 1)
    )
    monkeypatch.setattr(
        privacy_service.repository,
        "delete_project_memberships",
        _mutation("delete_project_memberships", 2),
    )
    monkeypatch.setattr(
        privacy_service.repository,
        "delete_external_identity_mappings",
        _mutation("delete_external_identity_mappings", 1),
    )
    monkeypatch.setattr(
        privacy_service.repository,
        "anonymize_agent_executions",
        _mutation("anonymize_agent_executions", 40),
    )
    monkeypatch.setattr(
        privacy_service.repository,
        "anonymize_invitations_for_email",
        _mutation("anonymize_invitations_for_email", 1),
    )
    monkeypatch.setattr(
        privacy_service.repository, "anonymize_user_record", _mutation("anonymize_user_record", 1)
    )
    monkeypatch.setattr(
        privacy_service.memory_repository,
        "hard_delete_user_scoped",
        _mutation("hard_delete_user_scoped_memory", 4),
    )

    async def fake_revoke_all_sessions(session, user_id, organization_id):
        state["revoked_sessions"] = int(state["revoked_sessions"]) + 1  # type: ignore[arg-type]
        return 3

    monkeypatch.setattr(
        privacy_service.auth_service, "revoke_all_sessions", fake_revoke_all_sessions
    )

    audit_events: list[dict] = []

    async def fake_record_audit_event(session, actor, **kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(privacy_service, "record_audit_event", fake_record_audit_event)
    state["audit_events"] = audit_events
    return state


async def _async(value):
    return value


# --- authorization ---------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_requires_tenancy_manage(repo):
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)
    with pytest.raises(PermissionDeniedError):
        await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())


@pytest.mark.asyncio
async def test_execute_requires_tenancy_manage(repo):
    """The permission check must reject before ANY mutation runs."""
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)
    with pytest.raises(PermissionDeniedError):
        await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert repo["calls"] == []


@pytest.mark.asyncio
async def test_deletion_always_uses_the_actors_own_organization(repo):
    """Cross-tenant deletion prevention: the organization is taken from the
    actor, never from a parameter, so there is no caller-supplied org to
    tamper with."""
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert result.organization_id == organization_id


# --- unsupported scopes ----------------------------------------------------


@pytest.mark.parametrize("scope", ["organization", "user_account"])
@pytest.mark.asyncio
async def test_unimplemented_scopes_are_rejected_loudly(repo, scope):
    """Silently downgrading an org-deletion request to user-scope would
    report success while leaving every document and embedding in place."""
    actor = _admin(uuid.uuid4())
    with pytest.raises(ValidationError, match="not implemented"):
        await privacy_service.execute_user_data_deletion(
            None, actor, uuid.uuid4(), scope=scope
        )
    assert repo["calls"] == []


# --- planning (dry run) ----------------------------------------------------


@pytest.mark.asyncio
async def test_plan_mutates_nothing(repo):
    actor = _admin(uuid.uuid4())
    await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    assert repo["calls"] == []
    assert repo["revoked_sessions"] == 0
    assert repo["audit_events"] == []


@pytest.mark.asyncio
async def test_plan_missing_user_raises_not_found(repo):
    repo["user_exists"] = False
    actor = _admin(uuid.uuid4())
    with pytest.raises(NotFoundError):
        await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())


@pytest.mark.asyncio
async def test_plan_classifies_organization_knowledge_as_retained(repo):
    """The single most important assertion in this file: a user deletion must
    never propose touching organization-owned knowledge."""
    actor = _admin(uuid.uuid4())
    plan = await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    retained = {s.category for s in plan.retain_steps}
    assert "organization_knowledge" in retained
    assert "incidents_reported" in retained
    assert "audit_trail" in retained
    # And none of those may appear in a mutating section.
    mutating = {s.category for s in plan.hard_delete_steps + plan.anonymize_steps}
    assert mutating.isdisjoint({"organization_knowledge", "incidents_reported", "audit_trail"})


@pytest.mark.asyncio
async def test_plan_classifies_access_artifacts_as_hard_delete(repo):
    actor = _admin(uuid.uuid4())
    plan = await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    hard = {s.category for s in plan.hard_delete_steps}
    assert hard == {
        "sessions",
        "organization_roles",
        "project_memberships",
        "sso_identity_links",
        "user_private_memory",
    }


@pytest.mark.asyncio
async def test_plan_classifies_user_record_and_telemetry_as_anonymize(repo):
    actor = _admin(uuid.uuid4())
    plan = await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    anon = {s.category for s in plan.anonymize_steps}
    assert anon == {"agent_execution_history", "invitations_received", "user_record"}


@pytest.mark.asyncio
async def test_plan_counts_exclude_retained_rows_from_blast_radius(repo):
    """`incidents_reported` is 7 in the fixture but is retained, so it must
    not inflate the count of rows the operation will actually change."""
    actor = _admin(uuid.uuid4())
    plan = await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    # 3 tokens + 1 role + 2 memberships + 1 mapping + 40 executions
    # + 4 private memories + 1 invite + 1 user
    assert plan.total_rows_affected == 53


@pytest.mark.asyncio
async def test_every_planned_step_carries_a_rationale(repo):
    """A plan whose steps cannot explain themselves is not reviewable."""
    actor = _admin(uuid.uuid4())
    plan = await privacy_service.plan_user_data_deletion(None, actor, uuid.uuid4())
    assert all(step.rationale.strip() for step in plan.steps)


# --- execution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_completes_and_runs_every_mutation(repo):
    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())

    assert result.status == "completed"
    assert result.failed_steps == []
    assert repo["calls"] == [
        "delete_refresh_tokens",
        "delete_user_roles",
        "delete_project_memberships",
        "delete_external_identity_mappings",
        "anonymize_agent_executions",
        "hard_delete_user_scoped_memory",
        "anonymize_invitations_for_email",
        "anonymize_user_record",
    ]


@pytest.mark.asyncio
async def test_execute_revokes_sessions_through_the_existing_auth_service(repo):
    """Reuse, not reimplementation: revocation goes through
    `core.auth.service.revoke_all_sessions` before token rows are deleted."""
    actor = _admin(uuid.uuid4())
    await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert repo["revoked_sessions"] == 1


@pytest.mark.asyncio
async def test_execute_reports_row_counts_split_by_action(repo):
    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert result.hard_deleted_row_count == 3 + 1 + 2 + 1 + 4
    assert result.anonymized_row_count == 40 + 1 + 1


# --- audit -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_event_records_the_operation_without_personal_data(repo):
    """The audit row must not preserve what the deletion just removed."""
    actor = _admin(uuid.uuid4())
    target = uuid.uuid4()
    await privacy_service.execute_user_data_deletion(None, actor, target)

    assert len(repo["audit_events"]) == 1
    event = repo["audit_events"][0]
    assert event["action"] == "privacy.user_data_deleted"
    assert event["resource_id"] == target
    serialized = str(event)
    assert "person@example.com" not in serialized
    assert "Deleted User" not in serialized
    assert event["metadata"]["status"] == "completed"
    assert event["metadata"]["retained_categories"]


# --- idempotency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_is_a_safe_noop(repo):
    """Re-running after a completed deletion must succeed, not error."""
    actor = _admin(uuid.uuid4())
    target = uuid.uuid4()
    first = await privacy_service.execute_user_data_deletion(None, actor, target)
    assert first.was_noop is False

    # Simulate post-deletion state: user anonymized, no resolvable email,
    # every mutation now matching zero rows.
    repo["is_anonymized"] = True
    repo["email"] = None
    for key in repo["counts"]:
        repo["counts"][key] = 0  # type: ignore[index]

    second = await privacy_service.execute_user_data_deletion(None, actor, target)
    assert second.status == "completed"
    assert second.was_noop is True
    assert second.failed_steps == []


@pytest.mark.asyncio
async def test_third_run_is_still_safe(repo):
    actor = _admin(uuid.uuid4())
    target = uuid.uuid4()
    repo["is_anonymized"] = True
    repo["email"] = None
    for _ in range(3):
        result = await privacy_service.execute_user_data_deletion(None, actor, target)
        assert result.status == "completed"
        assert result.was_noop is True


@pytest.mark.asyncio
async def test_already_anonymized_user_skips_invitation_lookup_cleanly(repo):
    """With no resolvable email there is nothing to search invitations by --
    recorded as a real succeeded zero-row step, so step lists stay
    comparable across runs."""
    repo["is_anonymized"] = True
    repo["email"] = None
    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())

    invitation_step = next(s for s in result.steps if s.category == "invitations_received")
    assert invitation_step.succeeded
    assert invitation_step.rows_affected == 0
    assert "anonymize_invitations_for_email" not in repo["calls"]
    # The step list still covers every category.
    assert len(result.steps) == 8


# --- partial failure -------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_step_does_not_prevent_the_others(repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("simulated derived-cleanup failure")

    monkeypatch.setattr(privacy_service.repository, "anonymize_agent_executions", boom)

    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())

    assert result.status == "partially_completed"
    assert [s.category for s in result.failed_steps] == ["agent_execution_history"]
    # The user record was still anonymized despite the earlier failure.
    assert "anonymize_user_record" in repo["calls"]


@pytest.mark.asyncio
async def test_partial_failure_is_never_reported_as_completed(repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(privacy_service.repository, "anonymize_user_record", boom)
    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert result.status != "completed"
    assert result.status == "partially_completed"


@pytest.mark.asyncio
async def test_all_steps_failing_reports_failed_not_partial(repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("everything is broken")

    for name in (
        "delete_refresh_tokens",
        "delete_user_roles",
        "delete_project_memberships",
        "delete_external_identity_mappings",
        "anonymize_agent_executions",
        "anonymize_invitations_for_email",
        "anonymize_user_record",
    ):
        monkeypatch.setattr(privacy_service.repository, name, boom)
    # The memory step lives in a different repository module -- it has to
    # fail too for "every step failed" to actually hold.
    monkeypatch.setattr(privacy_service.memory_repository, "hard_delete_user_scoped", boom)

    async def boom_revoke(*args, **kwargs):
        raise RuntimeError("revocation failed")

    monkeypatch.setattr(privacy_service.auth_service, "revoke_all_sessions", boom_revoke)

    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())
    assert result.status == "failed"
    assert len(result.failed_steps) == 8


@pytest.mark.asyncio
async def test_failure_reason_is_observable_per_step(repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(privacy_service.repository, "delete_user_roles", boom)
    actor = _admin(uuid.uuid4())
    result = await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())

    failed = next(s for s in result.failed_steps if s.category == "organization_roles")
    assert "disk on fire" in (failed.error or "")


@pytest.mark.asyncio
async def test_audit_event_records_failed_step_names(repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(privacy_service.repository, "delete_user_roles", boom)
    actor = _admin(uuid.uuid4())
    await privacy_service.execute_user_data_deletion(None, actor, uuid.uuid4())

    event = repo["audit_events"][0]
    assert event["metadata"]["failed_steps"] == ["organization_roles"]
    assert event["metadata"]["status"] == "partially_completed"


# --- retry after partial failure ------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_partial_failure_completes(repo, monkeypatch):
    """The supported recovery path: fix the cause, run it again."""
    calls = {"n": 0}

    async def fails_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return 0

    monkeypatch.setattr(privacy_service.repository, "anonymize_agent_executions", fails_once)

    actor = _admin(uuid.uuid4())
    target = uuid.uuid4()
    first = await privacy_service.execute_user_data_deletion(None, actor, target)
    assert first.status == "partially_completed"

    second = await privacy_service.execute_user_data_deletion(None, actor, target)
    assert second.status == "completed"
