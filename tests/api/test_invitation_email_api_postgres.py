"""The invitation-email endpoints end to end through the real FastAPI app.

Real request lifecycle (dependency commit, then the BackgroundTask), real
PostgreSQL as `ekip_app`: proves the background task sees the committed row and
records `sent` only after the (fake) provider accepted the message. Skipped
unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL migrated
database, with DATABASE_URL also pointing at it (the app's own engine).
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.core.tenancy import invitation_email
from app.database.session import get_db_session, set_tenant_context
from tests.core.tenancy.test_invitation_email_postgres import (  # noqa: F401  (fixtures + helpers)
    FakeSender,
    _actor,
    _configure,
    _end_cooldown,
    _setup_org,
    _APP_URL,
    _SUPER_URL,
    _is_local,
    env,
    inv,
)

pytestmark = pytest.mark.skipif(
    not (
        _SUPER_URL
        and _APP_URL
        and _is_local(_SUPER_URL)
        and _is_local(_APP_URL)
        and os.environ.get("DATABASE_URL", "").split("@")[-1].split("/")[0].split(":")[0] in {"localhost", "127.0.0.1"}
    ),
    reason="needs a LOCAL migrated database for EKIP_TEST_* and DATABASE_URL",
)


@pytest.fixture()
def api(monkeypatch):
    holder: dict[str, object] = {}

    async def identity(session=Depends(get_db_session)):
        await set_tenant_context(session, holder["org"])  # what get_current_identity does per request
        return holder["actor"]

    api_main.app.dependency_overrides[get_current_identity] = identity
    # One portal (event loop) for the whole test: the app's own engine pools
    # connections bound to the loop that opened them, and `TestClient` without
    # `with` would start a fresh loop per request.
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client, holder
    api_main.app.dependency_overrides.pop(get_current_identity, None)
    # Drop the pooled connections of the (now closed) loop; the next test opens its own.
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


@pytest.mark.asyncio
async def test_create_then_background_delivery_then_resend_and_copy_link(inv, api, monkeypatch) -> None:
    client, holder = api
    org_id, _r, admin_id, actor = None, None, None, None
    org_id, _r, admin_id = await _setup_org(inv, "api")
    actor = await _actor(inv, org_id, admin_id)
    holder.update(org=org_id, actor=actor)
    sender = FakeSender()
    monkeypatch.setattr(invitation_email, "get_email_sender", lambda: sender)
    inv.configure(True, invitation_resend_cooldown_seconds=60)
    base = f"/organizations/{org_id}/invitations"
    email = inv.email("api-user")

    created = client.post(base, json={"email": email, "grants_role": "engineer"})

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email_status"] == "queued" and body.get("token") is None
    # the BackgroundTask has run by now (TestClient waits for it) and saw the COMMITTED row
    row = (await inv.sup("SELECT email_status, email_attempts FROM invitations WHERE id=:i", i=body["id"]))[0]
    assert row.email_status == "sent" and row.email_attempts == 1
    assert len(sender.sent) == 1 and sender.sent[0].to == email
    listed = client.get(base).json()
    assert [i["email_status"] for i in listed if i["id"] == body["id"]] == ["sent"]
    assert "token" not in " ".join(k for i in listed for k, v in i.items() if v) or all(
        i.get("token") is None for i in listed
    )

    # resend: cooldown applies straight after sending ...
    cooldown = client.post(f"{base}/{body['id']}/resend", json={})
    assert cooldown.status_code == 429 and cooldown.json()["error_code"] == "invitation.resend_cooldown"
    await _end_cooldown(inv, body["id"])
    resent = client.post(f"{base}/{body['id']}/resend", json={"method": "email"})
    assert resent.status_code == 200 and resent.json()["token"] is None
    assert len(sender.sent) == 2
    old = re.search(r"#token=([\w\-]+)", sender.sent[0].text).group(1)
    new = re.search(r"#token=([\w\-]+)", sender.sent[1].text).group(1)
    assert old != new

    # ... "copy new link" returns the secret once and does not email
    link = client.post(f"{base}/{body['id']}/resend", json={"method": "link"})
    assert link.status_code == 200 and link.json()["token"] and link.json()["email_status"] == "not_sent"
    assert len(sender.sent) == 2

    # validation and authorization at the HTTP layer
    assert client.post(f"{base}/{body['id']}/resend", json={"method": "carrier-pigeon"}).status_code == 422
    assert client.post(f"{base}/00000000-0000-4000-8000-000000000000/resend", json={}).status_code == 404
    holder["actor"] = actor.model_copy(update={"permissions": frozenset({"incident:read"})})
    assert client.post(f"{base}/{body['id']}/resend", json={}).status_code == 403
    assert client.post(base, json={"email": inv.email("x"), "grants_role": "viewer"}).status_code == 403


@pytest.mark.asyncio
async def test_failed_delivery_is_reported_as_failed_over_the_api(inv, api, monkeypatch) -> None:
    from app.shared.email import EmailDeliveryError

    client, holder = api
    org_id, _r, admin_id = await _setup_org(inv, "apifail")
    holder.update(org=org_id, actor=await _actor(inv, org_id, admin_id))
    sender = FakeSender([EmailDeliveryError("SMTP delivery failed (SMTPServerDisconnected).")] * 3)
    monkeypatch.setattr(invitation_email, "get_email_sender", lambda: sender)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(invitation_email.asyncio, "sleep", no_sleep)
    inv.configure(True)
    created = client.post(f"/organizations/{org_id}/invitations", json={"email": inv.email("f"), "grants_role": "viewer"})
    assert created.status_code == 201
    listed = client.get(f"/organizations/{org_id}/invitations").json()
    assert listed[0]["email_status"] == "failed" and listed[0]["email_attempts"] == 3
    assert sender.sent == []


@pytest.mark.asyncio
async def test_without_email_the_api_returns_the_copyable_token(inv, api) -> None:
    client, holder = api
    inv.configure(False)
    org_id, _r, admin_id = await _setup_org(inv, "apinoemail")
    holder.update(org=org_id, actor=await _actor(inv, org_id, admin_id))
    created = client.post(f"/organizations/{org_id}/invitations", json={"email": inv.email("m"), "grants_role": "viewer"})
    assert created.status_code == 201
    assert created.json()["email_status"] == "not_configured" and created.json()["token"]
    refused = client.post(f"/organizations/{org_id}/invitations/{created.json()['id']}/resend", json={"method": "email"})
    assert refused.status_code == 503 and refused.json()["error_code"] == "email.not_configured"


@pytest.mark.asyncio
async def test_the_cap_surfaces_as_http_429(inv, api) -> None:
    client, holder = api
    inv.configure(True, invitation_max_per_hour=1)
    org_id, _r, admin_id = await _setup_org(inv, "apicap")
    holder.update(org=org_id, actor=await _actor(inv, org_id, admin_id))
    url = f"/organizations/{org_id}/invitations"
    assert client.post(url, json={"email": inv.email("c1"), "grants_role": "viewer"}).status_code == 201
    second = client.post(url, json={"email": inv.email("c2"), "grants_role": "viewer"})
    assert second.status_code == 429 and second.json()["error_code"] == "invitation.hourly_limit"
