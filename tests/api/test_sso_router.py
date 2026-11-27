"""HTTP wiring of the public SSO endpoints and the SSO settings API.

Real FastAPI app, stubbed service layer (the logic has its own tests): proves the
routes exist with the right shapes, the transaction secrets are not in any
response, errors map to the documented status codes, rate limits answer 429, and
only a `tenancy:manage` caller reaches `PATCH /organizations/{id}/sso`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.api.deps import get_current_identity
from app.core.auth import service as auth_service
from app.core.auth.schemas import SessionTokens, SSOAuthorizationRedirect, SSOOptions
from app.core.exceptions import NotFoundError, PermissionDeniedError, ServiceUnavailableError
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import SSOConfiguration
from app.database.session import get_db_session
from app.shared.rate_limiter import TokenBucketRateLimiter
from app.shared.schemas import ActorKind, Identity

ORG = uuid.uuid4()


@pytest.fixture()
def api(monkeypatch):
    async def fake_session():
        yield None

    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=ORG,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )
    api_main.app.dependency_overrides[get_db_session] = fake_session
    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    yield TestClient(api_main.app, raise_server_exceptions=False), actor
    api_main.app.dependency_overrides.clear()


def test_options_returns_only_availability_and_provider(api, monkeypatch) -> None:
    client, _ = api

    async def fake(session, slug):
        return SSOOptions(available=True, provider="okta", provider_label="Okta")

    monkeypatch.setattr(auth_service, "get_sso_options", fake)

    response = client.get("/auth/acme/sso-options")

    assert response.status_code == 200
    assert response.json() == {"available": True, "provider": "okta", "provider_label": "Okta"}


def test_options_needs_no_login_and_unknown_org_looks_like_no_sso(api, monkeypatch) -> None:
    client, _ = api
    api_main.app.dependency_overrides.pop(get_current_identity)  # anonymous

    async def fake(session, slug):
        return SSOOptions(available=False)

    monkeypatch.setattr(auth_service, "get_sso_options", fake)

    response = client.get("/auth/does-not-exist/sso-options")

    assert response.status_code == 200
    assert response.json() == {"available": False, "provider": None, "provider_label": None}


def test_begin_login_returns_url_state_and_handle_but_never_the_verifier(api, monkeypatch) -> None:
    client, _ = api
    seen = {}

    async def fake(session, slug, *, redirect_uri):
        seen.update(slug=slug, redirect_uri=redirect_uri)
        return SSOAuthorizationRedirect(authorization_url="https://idp.example/authorize?x=1", state="st", transaction_id="tx")

    monkeypatch.setattr(auth_service, "begin_sso_login", fake)

    response = client.get("/auth/acme/login", params={"redirect_uri": "http://localhost:5173/o/acme/callback"})

    assert response.status_code == 200
    assert set(response.json()) == {"authorization_url", "state", "transaction_id"}
    assert "code_verifier" not in response.text
    assert seen == {"slug": "acme", "redirect_uri": "http://localhost:5173/o/acme/callback"}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFoundError("x", error_code="sso.unavailable"), 404, "sso.unavailable"),
        (PermissionDeniedError("x", error_code="auth.redirect_uri_not_allowed"), 403, "auth.redirect_uri_not_allowed"),
        (ServiceUnavailableError("x", error_code="auth.idp_unreachable"), 503, "auth.idp_unreachable"),
    ],
)
def test_begin_login_error_mapping(api, monkeypatch, error, status, code) -> None:
    client, _ = api

    async def fake(session, slug, *, redirect_uri):
        raise error

    monkeypatch.setattr(auth_service, "begin_sso_login", fake)

    response = client.get("/auth/acme/login", params={"redirect_uri": "http://localhost:5173/o/acme/callback"})

    assert response.status_code == status and response.json()["error_code"] == code


def test_callback_accepts_the_new_body_and_rejects_the_old_one(api, monkeypatch) -> None:
    client, _ = api
    seen = {}

    async def fake(session, data, *, redirect_uri):
        seen["data"] = data
        return SessionTokens(access_token="a", refresh_token="r", expires_in=900)

    monkeypatch.setattr(auth_service, "complete_sso_login", fake)
    uri = {"redirect_uri": "http://localhost:5173/o/acme/callback"}

    ok = client.post("/auth/callback", params=uri, json={"org_slug": "acme", "code": "c", "state": "s", "transaction_id": "t"})
    legacy = client.post("/auth/callback", params=uri, json={"org_slug": "acme", "code": "c", "state": "s", "code_verifier": "v"})
    oversize = client.post("/auth/callback", params=uri, json={"org_slug": "acme", "code": "c", "state": "s", "transaction_id": "t" * 500})

    assert ok.status_code == 200 and ok.json()["access_token"] == "a"
    assert seen["data"].transaction_id == "t"
    assert legacy.status_code == 422  # a client still sending the browser-held verifier is refused
    assert oversize.status_code == 422


def test_sso_endpoints_are_rate_limited_per_ip(api, monkeypatch) -> None:
    client, _ = api

    async def fake_options(session, slug):
        return SSOOptions(available=False)

    async def fake_begin(session, slug, *, redirect_uri):
        raise NotFoundError("x", error_code="sso.unavailable")

    async def fake_complete(session, data, *, redirect_uri):
        raise PermissionDeniedError("x", error_code="auth.sso_transaction_invalid")

    monkeypatch.setattr(auth_service, "get_sso_options", fake_options)
    monkeypatch.setattr(auth_service, "begin_sso_login", fake_begin)
    monkeypatch.setattr(auth_service, "complete_sso_login", fake_complete)
    body = {"org_slug": "a", "code": "c", "state": "s", "transaction_id": "t"}
    uri = {"redirect_uri": "http://localhost:5173/o/a/callback"}

    options = [client.get("/auth/a/sso-options").status_code for _ in range(31)]
    begins = [client.get("/auth/a/login", params=uri).status_code for _ in range(21)]
    callbacks = [client.post("/auth/callback", params=uri, json=body).status_code for _ in range(21)]

    assert options[:30] == [200] * 30 and options[30] == 429
    assert begins[:20] == [404] * 20 and begins[20] == 429
    assert callbacks[:20] == [403] * 20 and callbacks[20] == 429


# ------------------------------------------------------------------ settings API


def _config(enabled: bool) -> SSOConfiguration:
    now = datetime.now(UTC)
    return SSOConfiguration(
        id=uuid.uuid4(), organization_id=ORG, provider="okta", protocol="oidc",
        issuer_url="https://acme.okta.com", client_id="cid", client_secret_ref="••••••••",
        enabled=enabled, created_at=now, updated_at=now,
    )


def test_patch_sso_updates_and_returns_enabled_flag_without_the_secret(api, monkeypatch) -> None:
    client, actor = api
    captured = {}

    async def fake(session, caller, organization_id, data):
        captured.update(caller=caller, org=organization_id, data=data)
        return _config(enabled=False)

    monkeypatch.setattr(tenancy_service, "update_sso_config", fake)

    response = client.patch(f"/organizations/{ORG}/sso", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False and response.json()["client_secret_ref"] == "••••••••"
    assert captured["org"] == ORG and captured["caller"] is actor
    assert captured["data"].enabled is False and captured["data"].confirm_identity_reset is False


def test_patch_sso_rejects_unknown_fields_and_bad_types(api, monkeypatch) -> None:
    client, _ = api

    async def fake(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError

    monkeypatch.setattr(tenancy_service, "update_sso_config", fake)

    assert client.patch(f"/organizations/{ORG}/sso", json={"protocol": "saml"}).status_code == 422
    assert client.patch(f"/organizations/{ORG}/sso", json={"organization_id": str(uuid.uuid4())}).status_code == 422
    assert client.patch(f"/organizations/{ORG}/sso", json={"enabled": "maybe"}).status_code == 422
    assert client.patch(f"/organizations/{ORG}/sso", json={"provider": "azure-ad"}).status_code == 422


def test_patch_sso_requires_a_login_and_the_service_enforces_permission(api, monkeypatch) -> None:
    client, _ = api

    async def deny(session, caller, organization_id, data):
        raise PermissionDeniedError("no", error_code="permission_denied")

    monkeypatch.setattr(tenancy_service, "update_sso_config", deny)
    assert client.patch(f"/organizations/{ORG}/sso", json={"enabled": False}).status_code == 403

    api_main.app.dependency_overrides.pop(get_current_identity)
    assert client.patch(f"/organizations/{ORG}/sso", json={"enabled": False}).status_code in (401, 403)
