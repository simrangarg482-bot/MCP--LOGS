"""Tests for the data-deletion endpoints on `app.api.routers.users`.

Same `TestClient` + `dependency_overrides` + stubbed-service style as
`tests/api/test_tenancy_router.py`. These cover the transport layer only --
that the right service function is called, with the caller's own identity,
and that a caller-supplied `user_id` can never redirect the operation at
another organization. The ownership/idempotency/partial-failure logic itself
is covered in `tests/core/privacy/`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import users as users_router
from app.core.privacy.schemas import DeletionPlan, DeletionResult, PlannedStep, StepResult
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )


@pytest.fixture()
def client():
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def _plan(organization_id: uuid.UUID, target: uuid.UUID) -> DeletionPlan:
    return DeletionPlan(
        scope="user_data",
        target_user_id=target,
        organization_id=organization_id,
        steps=[
            PlannedStep(
                category="sessions",
                action="hard_delete",
                table="refresh_tokens",
                count=2,
                rationale="ephemeral",
            ),
            PlannedStep(
                category="organization_knowledge",
                action="retain",
                table="documents",
                count=None,
                rationale="organization-owned",
            ),
        ],
    )


def _result(organization_id: uuid.UUID, target: uuid.UUID) -> DeletionResult:
    return DeletionResult(
        scope="user_data",
        target_user_id=target,
        organization_id=organization_id,
        status="completed",
        steps=[
            StepResult(
                category="sessions", action="hard_delete", succeeded=True, rows_affected=2
            )
        ],
        executed_at=_NOW,
    )


def test_plan_endpoint_is_a_dry_run_returning_the_plan(client, monkeypatch):
    test_client, actor = client
    target = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_plan(session, passed_actor, user_id, **kwargs):
        captured["actor"] = passed_actor
        captured["user_id"] = user_id
        return _plan(actor.organization_id, target)

    monkeypatch.setattr(users_router.privacy_service, "plan_user_data_deletion", fake_plan)

    response = test_client.get(f"/users/{target}/data-deletion/plan")

    assert response.status_code == 200
    assert captured["actor"] is actor
    assert captured["user_id"] == target
    payload = response.json()
    assert payload["scope"] == "user_data"
    # The retained category must be visible in the dry run -- the whole
    # point is showing an operator what will NOT be touched.
    categories = {step["category"]: step["action"] for step in payload["steps"]}
    assert categories["organization_knowledge"] == "retain"


def test_execute_endpoint_calls_the_service_with_the_callers_identity(client, monkeypatch):
    test_client, actor = client
    target = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_execute(session, passed_actor, user_id, **kwargs):
        captured["actor"] = passed_actor
        captured["user_id"] = user_id
        return _result(actor.organization_id, target)

    monkeypatch.setattr(users_router.privacy_service, "execute_user_data_deletion", fake_execute)

    response = test_client.post(f"/users/{target}/data-deletion")

    assert response.status_code == 200
    assert captured["actor"] is actor
    assert response.json()["status"] == "completed"


def test_execute_endpoint_never_accepts_a_caller_supplied_organization(client, monkeypatch):
    """Cross-tenant prevention at the transport layer: the endpoint takes no
    organization parameter at all, so the service always derives it from the
    authenticated identity."""
    test_client, actor = client
    target = uuid.uuid4()
    seen: dict[str, object] = {}

    async def fake_execute(session, passed_actor, user_id, **kwargs):
        seen["organization_id"] = passed_actor.organization_id
        return _result(passed_actor.organization_id, target)

    monkeypatch.setattr(users_router.privacy_service, "execute_user_data_deletion", fake_execute)

    # Attempt to smuggle a different organization in via query string and body.
    other_org = uuid.uuid4()
    response = test_client.post(
        f"/users/{target}/data-deletion?organization_id={other_org}",
        json={"organization_id": str(other_org)},
    )

    assert response.status_code == 200
    assert seen["organization_id"] == actor.organization_id
    assert seen["organization_id"] != other_org


def test_deletion_result_exposes_status_for_partial_failure(client, monkeypatch):
    """A caller must be able to see that a run did not fully succeed."""
    test_client, actor = client
    target = uuid.uuid4()

    async def fake_execute(session, passed_actor, user_id, **kwargs):
        return DeletionResult(
            scope="user_data",
            target_user_id=target,
            organization_id=passed_actor.organization_id,
            status="partially_completed",
            steps=[
                StepResult(
                    category="sessions", action="hard_delete", succeeded=True, rows_affected=1
                ),
                StepResult(
                    category="user_record",
                    action="anonymize",
                    succeeded=False,
                    error="database unavailable",
                ),
            ],
            executed_at=_NOW,
        )

    monkeypatch.setattr(users_router.privacy_service, "execute_user_data_deletion", fake_execute)

    payload = test_client.post(f"/users/{target}/data-deletion").json()
    assert payload["status"] == "partially_completed"
    failed = [s for s in payload["steps"] if not s["succeeded"]]
    assert len(failed) == 1
    assert failed[0]["error"] == "database unavailable"


def test_no_delete_verb_endpoint_exists_for_users():
    """Deliberate API-design assertion: this feature does not remove a
    `users` row (three RESTRICT foreign keys make that impossible), so it
    must not advertise `DELETE /users/{id}`.

    Introspected via `app.openapi()["paths"]`. An earlier version of this
    test walked `app.routes`, which in this FastAPI version yields
    `_IncludedRouter` objects with no `.path`/`.methods` -- so it built an
    empty list and passed no matter what. Asserting over the OpenAPI schema
    is the version-stable way to make this check real.
    """
    paths = api_main.app.openapi()["paths"]
    operations = {
        (path, method.upper()) for path, methods in paths.items() for method in methods
    }
    # The check itself must be non-vacuous: prove the user surface is present.
    assert any(path.startswith("/users") for path, _ in operations), "user routes missing"
    assert ("/users/{user_id}", "DELETE") not in operations
    # The real deletion surface is a POST to an explicit sub-resource.
    assert ("/users/{user_id}/data-deletion", "POST") in operations
