"""HTTP contract of the password endpoints.

The service layer is stubbed (its behaviour is proven in `tests/core/auth/test_password_*`, including
against real Postgres). What is pinned here: status codes and generic non-enumerating bodies, mail
sent only AFTER the response and only when a link was actually issued, no token in any response,
fail-closed when email is not configured, authentication on change, schema bounds, and per-IP /
per-user rate limits.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import auth as auth_router
from app.core.auth import password_reset
from app.core.auth import service as auth_service
from app.core.auth.schemas import SessionTokens
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.database.session import get_db_session
from app.shared.rate_limiter import TokenBucketRateLimiter
from app.shared.schemas import ActorKind, Identity

ORG = uuid.uuid4()


def _delivery(token="RAW-RESET-TOKEN-NEVER-IN-A-RESPONSE"):
    return password_reset.PendingResetDelivery(uuid.uuid4(), "person@corp.test", token)


@pytest.fixture()
def api(monkeypatch):
    async def fake_session():
        yield None

    api_main.app.dependency_overrides[get_db_session] = fake_session
    monkeypatch.setattr("app.api.deps.email_delivery_configured", lambda: True)
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    sent: list[object] = []

    async def fake_reset_email(delivery, **kwargs):
        sent.append(("reset", delivery))

    async def fake_notice(notice, **kwargs):
        sent.append(("notice", notice))

    monkeypatch.setattr(auth_router.password_reset, "deliver_reset_email", fake_reset_email)
    monkeypatch.setattr(auth_router.password_reset, "deliver_password_changed_notice", fake_notice)
    yield TestClient(api_main.app, raise_server_exceptions=False), sent
    api_main.app.dependency_overrides.clear()


# ------------------------------------------------------------------ forgot


def test_forgot_is_202_with_the_same_body_whether_or_not_a_link_was_issued(api, monkeypatch) -> None:
    client, sent = api
    outcomes = iter([_delivery(), None])

    async def fake_request(session, *, email):
        return next(outcomes)

    monkeypatch.setattr(password_reset, "request_reset", fake_request)

    issued = client.post("/auth/password/forgot", json={"email": "person@corp.test"})
    nothing = client.post("/auth/password/forgot", json={"email": "nobody@corp.test"})

    assert issued.status_code == nothing.status_code == 202
    assert issued.json() == nothing.json()  # indistinguishable
    assert "RAW-RESET-TOKEN" not in issued.text
    assert [kind for kind, _ in sent] == ["reset"]  # mail only for the account that got a link


def test_forgot_fails_closed_without_email_and_before_looking_at_the_address(api, monkeypatch) -> None:
    client, sent = api
    monkeypatch.setattr("app.api.deps.email_delivery_configured", lambda: False)

    async def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError

    monkeypatch.setattr(password_reset, "request_reset", must_not_run)

    response = client.post("/auth/password/forgot", json={"email": "anyone@corp.test"})

    assert response.status_code == 503 and response.json()["error_code"] == "email.not_configured"


def test_forgot_validates_the_payload_shape_only(api, monkeypatch) -> None:
    client, _ = api

    async def fake_request(session, *, email):
        return None

    monkeypatch.setattr(password_reset, "request_reset", fake_request)
    assert client.post("/auth/password/forgot", json={}).status_code == 422
    assert client.post("/auth/password/forgot", json={"email": "x" * 400}).status_code == 422
    assert client.post("/auth/password/forgot", json={"email": "not-an-email"}).status_code == 202  # the server decides, silently


def test_forgot_is_rate_limited_per_ip(api, monkeypatch) -> None:
    client, _ = api

    async def fake_request(session, *, email):
        return None

    monkeypatch.setattr(password_reset, "request_reset", fake_request)
    codes = [client.post("/auth/password/forgot", json={"email": "a@b.test"}).status_code for _ in range(6)]
    assert codes[:5] == [202] * 5 and codes[5] == 429


# ------------------------------------------------------------------ reset


def test_reset_success_returns_no_tokens_and_schedules_the_notice(api, monkeypatch) -> None:
    client, sent = api

    async def fake_reset(session, *, token, new_password):
        assert token == "tok" and new_password == "a brand new password"
        return password_reset.PendingPasswordNotice(email="person@corp.test", how="reset", when=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    monkeypatch.setattr(password_reset, "reset_password", fake_reset)

    response = client.post("/auth/password/reset", json={"token": "tok", "new_password": "a brand new password"})

    assert response.status_code == 200 and response.json()["status"] == "password_reset"
    assert not {"access_token", "refresh_token", "token"} & set(response.json())
    assert [kind for kind, _ in sent] == ["notice"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ValidationError("x", error_code="auth.reset_token_invalid"), 400, "auth.reset_token_invalid"),
        (ValidationError("x", error_code="auth.reset_token_expired"), 400, "auth.reset_token_expired"),
        (ConflictError("x", error_code="auth.reset_token_used"), 409, "auth.reset_token_used"),
        (ValidationError("x", error_code="auth.password_too_short"), 400, "auth.password_too_short"),
    ],
)
def test_reset_error_mapping_and_no_notice_on_failure(api, monkeypatch, error, status, code) -> None:
    client, sent = api

    async def fake_reset(session, *, token, new_password):
        raise error

    monkeypatch.setattr(password_reset, "reset_password", fake_reset)

    response = client.post("/auth/password/reset", json={"token": "t", "new_password": "whatever it is here"})

    assert response.status_code == status and response.json()["error_code"] == code
    assert sent == []


def test_reset_payload_bounds_and_rate_limit(api, monkeypatch) -> None:
    client, _ = api

    async def fake_reset(session, *, token, new_password):
        raise ValidationError("x", error_code="auth.reset_token_invalid")

    monkeypatch.setattr(password_reset, "reset_password", fake_reset)
    assert client.post("/auth/password/reset", json={"token": "", "new_password": "x"}).status_code == 422
    assert client.post("/auth/password/reset", json={"token": "t" * 600, "new_password": "x"}).status_code == 422
    assert client.post("/auth/password/reset", json={"token": "t", "new_password": "p" * 2000}).status_code == 422
    # the 422s above also spent budget (the limiter runs before body validation): start clean
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    codes = [client.post("/auth/password/reset", json={"token": "t", "new_password": "some password here"}).status_code for _ in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429


# ------------------------------------------------------------------ change


def _actor():
    return Identity(kind=ActorKind.USER, subject=str(uuid.uuid4()), organization_id=ORG, user_id=uuid.uuid4())


def test_change_requires_a_signed_in_user(api, monkeypatch) -> None:
    client, _ = api

    async def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError

    monkeypatch.setattr(auth_service, "change_password", must_not_run)
    response = client.post("/auth/password/change", json={"current_password": "a", "new_password": "b"})
    assert response.status_code in (401, 403)


def test_change_returns_the_new_session_and_schedules_the_notice(api, monkeypatch) -> None:
    client, sent = api
    actor = _actor()
    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    seen = {}

    async def fake_change(session, *, user_id, organization_id, data):
        seen.update(user_id=user_id, organization_id=organization_id, data=data)
        return (
            SessionTokens(access_token="new-access", refresh_token="new-refresh", expires_in=900),
            password_reset.PendingPasswordNotice(email="p@corp.test", how="change", when=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)),
        )

    monkeypatch.setattr(auth_service, "change_password", fake_change)

    response = client.post("/auth/password/change", json={"current_password": "old password here", "new_password": "new password here!"})

    assert response.status_code == 200 and response.json()["access_token"] == "new-access"
    assert seen["user_id"] == actor.user_id and seen["organization_id"] == ORG  # identity from the token, never the body
    assert [kind for kind, _ in sent] == ["notice"]


def test_change_rejects_a_service_identity_and_wrong_current_password(api, monkeypatch) -> None:
    client, sent = api
    service_identity = Identity(kind=ActorKind.AGENT, subject="agent-x", organization_id=ORG)
    api_main.app.dependency_overrides[get_current_identity] = lambda: service_identity
    assert client.post("/auth/password/change", json={"current_password": "a", "new_password": "b"}).status_code == 400

    api_main.app.dependency_overrides[get_current_identity] = lambda: _actor()

    async def fake_change(session, **kwargs):
        raise PermissionDeniedError("no", error_code="auth.current_password_incorrect")

    monkeypatch.setattr(auth_service, "change_password", fake_change)
    response = client.post("/auth/password/change", json={"current_password": "a", "new_password": "b"})
    assert response.status_code == 403 and response.json()["error_code"] == "auth.current_password_incorrect"
    assert sent == []


def test_change_is_rate_limited_per_user_not_per_ip(api, monkeypatch) -> None:
    client, _ = api
    actor = _actor()
    api_main.app.dependency_overrides[get_current_identity] = lambda: actor

    async def fake_change(session, **kwargs):
        raise PermissionDeniedError("no", error_code="auth.current_password_incorrect")

    monkeypatch.setattr(auth_service, "change_password", fake_change)
    body = {"current_password": "guess", "new_password": "x"}
    codes = [client.post("/auth/password/change", json=body).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
    # a different user is not throttled by this one's guesses
    other = _actor()
    api_main.app.dependency_overrides[get_current_identity] = lambda: other
    assert client.post("/auth/password/change", json=body).status_code == 403
