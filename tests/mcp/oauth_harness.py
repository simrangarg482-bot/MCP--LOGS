"""Shared harness for the MCP OAuth tests.

Drives the REAL `mcp` SDK Starlette app (`mcp_server.streamable_http_app`) over
HTTP with `starlette.testclient.TestClient`, so the SDK's own handlers
(`/register`, `/authorize`, `/token`, `/revoke`, bearer middleware) and EKIP's
`EkipOAuthProvider` / `core.auth.service` token code all run unmodified.

Only *persistence* is replaced with in-memory stand-ins (no database exists in
these unit tests): the OAuth client registry (`core.mcp_oauth.service`), the
refresh-token table (`core.auth.repository`) and the user/identity lookup.
Token signing, scope/audience handling, redirect-URI policy, CSRF checks and
the refresh-binding rules are all real code under test.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from app.core.auth import service as auth_service
from app.core.mcp_oauth import service as mcp_oauth_service
from app.core.users import service as users_service
from app.mcp.servers import server as server_module

BASE_URL = str(server_module._public_base_url).rstrip("/")
REDIRECT_URI = "http://localhost:3334/oauth/callback"  # mcp-remote's loopback callback


class FakeStore:
    """In-memory replacement for the three persistence seams."""

    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.client_created_at: dict[str, float] = {}
        self.refresh_rows: dict[str, SimpleNamespace] = {}  # token_hash -> row
        self.user_id = uuid.uuid4()
        self.organization_id = uuid.uuid4()


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()

    # --- OAuth client registry (core.mcp_oauth.service) ---------------------
    async def get_registered_client(session, client_id):
        row = fake.clients.get(client_id)
        return dict(row) if row else None

    async def register_oauth_client(session, client_info):
        fake.clients[client_info["client_id"]] = dict(client_info)
        fake.client_created_at[client_info["client_id"]] = time.time()

    async def count_recent_registrations(session, *, within_seconds):
        cutoff = time.time() - within_seconds
        return sum(1 for t in fake.client_created_at.values() if t >= cutoff)

    monkeypatch.setattr(mcp_oauth_service, "count_recent_registrations", count_recent_registrations)
    monkeypatch.setattr(mcp_oauth_service, "get_registered_client", get_registered_client)
    monkeypatch.setattr(mcp_oauth_service, "register_oauth_client", register_oauth_client)

    # --- refresh_tokens (core.auth.repository) ------------------------------
    repo = auth_service.repository

    async def insert_refresh_token(session, **kw):
        kw.setdefault("oauth_client_id", None)
        row = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, **kw)
        fake.refresh_rows[kw["token_hash"]] = row
        return row

    async def resolve_org(session, token_hash):
        row = fake.refresh_rows.get(token_hash)
        return row.organization_id if row else None

    async def get_by_hash(session, token_hash):
        return fake.refresh_rows.get(token_hash)

    async def revoke_refresh_token(session, refresh_token_id, *, revoked_at):
        for row in fake.refresh_rows.values():
            if row.id == refresh_token_id:
                row.revoked_at = revoked_at

    async def revoke_refresh_token_if_active(session, refresh_token_id, *, revoked_at):
        for row in fake.refresh_rows.values():
            if row.id == refresh_token_id and row.revoked_at is None:
                row.revoked_at = revoked_at
                return True
        return False

    async def revoke_family(session, family_id, *, revoked_at):
        for row in fake.refresh_rows.values():
            if row.family_id == family_id and row.revoked_at is None:
                row.revoked_at = revoked_at

    monkeypatch.setattr(repo, "insert_refresh_token", insert_refresh_token)
    monkeypatch.setattr(repo, "resolve_refresh_token_organization_id", resolve_org)
    monkeypatch.setattr(repo, "get_refresh_token_by_hash", get_by_hash)
    monkeypatch.setattr(repo, "revoke_refresh_token", revoke_refresh_token)
    monkeypatch.setattr(repo, "revoke_refresh_token_if_active", revoke_refresh_token_if_active)
    monkeypatch.setattr(repo, "revoke_family", revoke_family)

    async def no_tenant_context(session, organization_id):
        return None

    monkeypatch.setattr(auth_service, "set_tenant_context", no_tenant_context)

    async def organization_is_active(session, organization_id):
        return 0  # epoch; `_assert_organization_not_suspended` needs a database; covered against Postgres

    monkeypatch.setattr(auth_service, "_assert_organization_not_suspended", organization_is_active)

    # --- users / identity ----------------------------------------------------
    async def resolve_identity(session, user_id, organization_id, **kwargs):
        return SimpleNamespace(user_id=user_id, organization_id=organization_id)

    async def email_state(session, user_id):
        return SimpleNamespace(is_active=True, email_verified_at=datetime.now(UTC))

    monkeypatch.setattr(users_service, "resolve_identity", resolve_identity)
    monkeypatch.setattr(users_service, "get_user_email_state_by_id", email_state)

    # --- server wiring -------------------------------------------------------
    @asynccontextmanager
    async def session_factory():
        yield SimpleNamespace()

    monkeypatch.setattr(server_module, "session_factory", session_factory)
    monkeypatch.setattr(server_module, "set_tenant_context", no_tenant_context)

    # Provider state is process-global; isolate every test.
    provider = server_module._oauth_provider
    monkeypatch.setattr(provider, "_pending_flows", {})
    monkeypatch.setattr(provider, "_auth_codes", {})
    monkeypatch.setattr(provider, "_issued_sessions", {})
    return fake


@pytest.fixture
def client(store):
    host = urlparse(BASE_URL).netloc
    hostname = urlparse(BASE_URL).hostname or "localhost"
    app = server_module.mcp_server.streamable_http_app(
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[hostname, host, f"{hostname}:*"]
        ),
    )
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as test_client:
        yield test_client


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client, *, redirect_uri: str = REDIRECT_URI, **extra):
    body = {
        "client_name": "Test Client",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        **extra,
    }
    return client.post("/register", json=body)


def registered_client(client, **kw) -> dict:
    response = register(client, **kw)
    assert response.status_code == 201, response.text
    return response.json()


def rest_access_token(store: FakeStore, **kw) -> str:
    """A plain REST-style EKIP access token (what the confirmation page asks
    the human to paste)."""
    token, _, _ = auth_service._issue_access_token(store.user_id, store.organization_id, **kw)
    return token


def mcp_access_token(store: FakeStore, **overrides) -> str:
    """An access token shaped like the ones the MCP OAuth bridge issues
    (`scope`, `aud` and `iss` set); keyword overrides tamper with one field."""
    resource = BASE_URL
    fields = {"scope": "ekip", "audience": resource, "issuer": resource, **overrides}
    token, _, _ = auth_service._issue_access_token(store.user_id, store.organization_id, **fields)
    return token


def start_authorize(
    client,
    info: dict,
    *,
    challenge: str,
    scope: str | None = None,
    resource: str | None = None,
    state="st",
):
    params = {
        "response_type": "code",
        "client_id": info["client_id"],
        "redirect_uri": info["redirect_uris"][0],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope is not None:
        params["scope"] = scope
    if resource is not None:
        params["resource"] = resource
    return client.get("/authorize", params=params)


def confirm(client, authorize_response, pasted_token: str, **form_overrides):
    """Follow `/authorize` to the consent page and submit it like a browser:
    GET (collect cookie + hidden CSRF field), then POST."""
    assert authorize_response.status_code == 302, authorize_response.text
    location = authorize_response.headers["location"]
    flow_id = parse_qs(urlparse(location).query)["flow_id"][0]
    page = client.get(location)
    form = {"flow_id": flow_id, "ekip_access_token": pasted_token}
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    if match:
        form["csrf_token"] = match.group(1)
    form.update(form_overrides)
    return client.post("/ekip/oauth/authorize", data=form)


def exchange_code(client, info: dict, code: str, verifier: str, **extra):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": info["redirect_uris"][0],
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "code_verifier": verifier,
            **extra,
        },
    )


def full_flow(client, store: FakeStore, info: dict | None = None, **authorize_kw):
    """DCR -> authorize -> confirm -> token. Returns (client_info, token_json)."""
    info = info or registered_client(client)
    verifier, challenge = pkce_pair()
    authorize_response = start_authorize(client, info, challenge=challenge, **authorize_kw)
    confirmed = confirm(client, authorize_response, rest_access_token(store))
    assert confirmed.status_code == 302, confirmed.text
    code = parse_qs(urlparse(confirmed.headers["location"]).query)["code"][0]
    token_response = exchange_code(client, info, code, verifier)
    assert token_response.status_code == 200, token_response.text
    return info, token_response.json()


def refresh_with(client, info: dict, refresh_token: str):
    return client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
        },
    )


def mcp_initialize(client, bearer: str | None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )


def decode_unverified(token: str) -> dict:
    from jose import jwt

    return jwt.get_unverified_claims(token)
