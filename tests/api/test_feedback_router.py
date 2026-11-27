"""Tests for `app.api.routers.feedback` -- transport layer only (the
ownership/tenancy logic is covered in `tests/agents/feedback/`).

Same `TestClient` + `dependency_overrides` + stubbed-service style as
`tests/api/test_memory_router.py`. The structural-authorization tests matter
most: the API must offer no parameter through which a caller could name
another organization or user.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.agents.feedback.schemas import Feedback
from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import feedback as feedback_router
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity

_NOW = datetime(2026, 11, 28, tzinfo=UTC)


@pytest.fixture()
def client():
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session
    yield TestClient(api_main.app), actor
    api_main.app.dependency_overrides.clear()


def _feedback(execution_id: uuid.UUID, **overrides) -> Feedback:
    values = dict(
        id=uuid.uuid4(),
        execution_id=execution_id,
        rating="helpful",
        reason_code=None,
        comment=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    values.update(overrides)
    return Feedback(**values)


def test_put_submits_feedback_for_the_path_execution(client, monkeypatch) -> None:
    test_client, actor = client
    execution_id = uuid.uuid4()
    captured: dict = {}

    async def fake_submit(session, passed_actor, passed_execution_id, data):
        captured.update(actor=passed_actor, execution_id=passed_execution_id, data=data)
        return _feedback(passed_execution_id, rating=data.rating, comment=data.comment)

    monkeypatch.setattr(feedback_router.feedback_service, "submit_feedback", fake_submit)

    response = test_client.put(
        f"/ask/executions/{execution_id}/feedback",
        json={"rating": "not_helpful", "reason_code": "outdated", "comment": "old docs"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == str(execution_id)
    assert body["rating"] == "not_helpful"
    assert captured["actor"] is actor
    assert captured["execution_id"] == execution_id
    assert captured["data"].reason_code == "outdated"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rating": "meh"},
        {"rating": "helpful", "reason_code": "nonsense"},
        {"rating": "helpful", "comment": "x" * 2001},
    ],
)
def test_invalid_bodies_are_rejected_with_422_before_the_service(client, monkeypatch, body) -> None:
    test_client, _ = client

    async def must_not_run(*args, **kwargs):
        raise AssertionError("service reached with an invalid body")

    monkeypatch.setattr(feedback_router.feedback_service, "submit_feedback", must_not_run)

    response = test_client.put(f"/ask/executions/{uuid.uuid4()}/feedback", json=body)

    assert response.status_code == 422


def test_a_malformed_execution_id_is_a_422(client) -> None:
    test_client, _ = client

    response = test_client.put("/ask/executions/not-a-uuid/feedback", json={"rating": "helpful"})

    assert response.status_code == 422


def test_no_organization_or_user_can_be_supplied_by_the_client(client, monkeypatch) -> None:
    test_client, _ = client

    async def must_not_run(*args, **kwargs):
        raise AssertionError("service reached")

    monkeypatch.setattr(feedback_router.feedback_service, "submit_feedback", must_not_run)

    for extra in ("organization_id", "user_id", "execution_id"):
        response = test_client.put(
            f"/ask/executions/{uuid.uuid4()}/feedback",
            json={"rating": "helpful", extra: str(uuid.uuid4())},
        )
        assert response.status_code == 422, extra


def test_unknown_execution_maps_to_404(client, monkeypatch) -> None:
    test_client, _ = client

    async def fake_submit(*args, **kwargs):
        raise NotFoundError("Execution not found.", error_code="feedback.execution_not_found")

    monkeypatch.setattr(feedback_router.feedback_service, "submit_feedback", fake_submit)

    response = test_client.put(f"/ask/executions/{uuid.uuid4()}/feedback", json={"rating": "helpful"})

    assert response.status_code == 404
    assert response.json()["error_code"] == "feedback.execution_not_found"


def test_unrateable_execution_maps_to_409(client, monkeypatch) -> None:
    test_client, _ = client

    async def fake_submit(*args, **kwargs):
        raise ConflictError("no", error_code="feedback.execution_not_rateable")

    monkeypatch.setattr(feedback_router.feedback_service, "submit_feedback", fake_submit)

    response = test_client.put(f"/ask/executions/{uuid.uuid4()}/feedback", json={"rating": "helpful"})

    assert response.status_code == 409


def test_get_returns_the_callers_rating_or_null(client, monkeypatch) -> None:
    test_client, _ = client
    execution_id = uuid.uuid4()
    answers = iter([_feedback(execution_id, rating="not_helpful"), None])

    async def fake_get(session, actor, passed_execution_id):
        return next(answers)

    monkeypatch.setattr(feedback_router.feedback_service, "get_my_feedback", fake_get)

    first = test_client.get(f"/ask/executions/{execution_id}/feedback")
    second = test_client.get(f"/ask/executions/{execution_id}/feedback")

    assert first.status_code == 200 and first.json()["rating"] == "not_helpful"
    assert second.status_code == 200 and second.json() is None


def test_insights_are_passed_the_window_and_denied_without_permission(client, monkeypatch) -> None:
    test_client, _ = client
    seen: dict = {}

    async def fake_insights(session, actor, *, window_days):
        seen["window_days"] = window_days
        raise PermissionDeniedError("no", error_code="permission_denied")

    monkeypatch.setattr(feedback_router.feedback_service, "get_insights", fake_insights)

    response = test_client.get("/feedback/insights?window_days=7")

    assert response.status_code == 403
    assert seen["window_days"] == 7
    assert test_client.get("/feedback/insights?window_days=0").status_code == 422
    assert test_client.get("/feedback/insights?window_days=366").status_code == 422


def test_there_is_no_endpoint_listing_individual_ratings(client) -> None:
    test_client, _ = client

    for path in ("/feedback", "/ask/feedback", "/feedback/all"):
        assert test_client.get(path).status_code in (404, 405)


def test_unauthenticated_requests_are_rejected() -> None:
    """No dependency overrides: the real authentication dependency runs."""
    api_main.app.dependency_overrides.clear()
    unauthenticated = TestClient(api_main.app)

    put = unauthenticated.put(f"/ask/executions/{uuid.uuid4()}/feedback", json={"rating": "helpful"})
    get = unauthenticated.get(f"/ask/executions/{uuid.uuid4()}/feedback")
    insights = unauthenticated.get("/feedback/insights")

    # The bearer-token scheme answers a request with no credentials with
    # 401 or 403 depending on the framework version; either way: refused.
    for response in (put, get, insights):
        assert response.status_code in (401, 403)
