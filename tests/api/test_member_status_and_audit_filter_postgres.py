"""Backend support for the Phase 3 UI against real PostgreSQL over real HTTP: the members list carries each
member's organization-scoped access state, and the audit log can be filtered by an action prefix.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.shared.rate_limiter import TokenBucketRateLimiter
from tests.api.test_member_offboarding_api_postgres import _admin, _h, _login  # noqa: F401  (helpers)
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _org,
    _user,
    env,
    pw,
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
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


@pytest_asyncio.fixture(autouse=True)
async def _migrated(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='organization_member_states'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


@pytest.mark.asyncio
async def test_the_members_list_reports_each_members_access_state_and_source(pw, api) -> None:  # noqa: F811
    org, other = await _org(pw, "ml"), await _org(pw, "mlother")
    await _admin(pw, "mladmin", org)
    by_admin = await _user(pw, "mlbyadmin", org)
    by_scim = await _user(pw, "mlbyscim", org)
    active = await _user(pw, "mlactive", org)
    foreign = await _user(pw, "mlforeign", other)
    await pw.sup("INSERT INTO organization_member_states (organization_id, user_id, status, source) VALUES (:o,:u,'deactivated','admin')", o=org, u=by_admin)
    await pw.sup("INSERT INTO organization_member_states (organization_id, user_id, status, source) VALUES (:o,:u,'deactivated','scim')", o=org, u=by_scim)
    await pw.sup("INSERT INTO organization_member_states (organization_id, user_id, status, source) VALUES (:o,:u,'deactivated','admin')", o=other, u=foreign)

    t = _login(api, pw, "mladmin")
    rows = {r["id"]: r for r in api.get(f"/organizations/{org}/members", headers=_h(t)).json()}
    assert (rows[str(by_admin)]["membership_status"], rows[str(by_admin)]["membership_source"]) == ("deactivated", "admin")
    assert (rows[str(by_scim)]["membership_status"], rows[str(by_scim)]["membership_source"]) == ("deactivated", "scim")
    assert (rows[str(active)]["membership_status"], rows[str(active)]["membership_source"]) == ("active", None)
    assert str(foreign) not in rows  # another organization's state never leaks in
    assert all("reason" not in r for r in rows.values())  # the reason is not part of the list


@pytest.mark.asyncio
async def test_the_audit_log_filters_by_action_prefix_literally_and_per_organization(pw, api) -> None:  # noqa: F811
    org, other = await _org(pw, "af"), await _org(pw, "afother")
    admin = await _admin(pw, "afadmin", org)
    for action in ("member.deactivate", "member.reactivate", "scim.user_created", "scim.token_created", "incident.create", "x%y.z", "x_y.z"):
        await pw.sup(
            "INSERT INTO audit_logs (organization_id, actor, action, resource_type) VALUES (:o,:a,:x,'t')", o=org, a=f"user:{admin}", x=action
        )
    await pw.sup("INSERT INTO audit_logs (organization_id, actor, action, resource_type) VALUES (:o,'user:z','scim.secret','t')", o=other)
    t = _login(api, pw, "afadmin")

    def actions(prefix):
        r = api.get(f"/organizations/{org}/audit", headers=_h(t), params={"action_prefix": prefix, "limit": 200})
        assert r.status_code == 200, r.text
        return {e["action"] for e in r.json()}

    assert actions("scim.") == {"scim.user_created", "scim.token_created"}  # the other organization's scim.secret is invisible
    assert actions("member.") == {"member.deactivate", "member.reactivate"}
    assert actions("x%") == {"x%y.z"}  # % is literal, not a wildcard
    assert actions("x_") == {"x_y.z"}  # _ is literal, not a single-character wildcard
    assert actions("nothing.") == set()
    everything = api.get(f"/organizations/{org}/audit", headers=_h(t), params={"limit": 200}).json()
    assert {"incident.create", "scim.user_created"} <= {e["action"] for e in everything}  # no filter: everything
    assert api.get(f"/organizations/{org}/audit", headers=_h(t), params={"action_prefix": "x" * 65}).status_code == 422
    assert uuid.UUID(str(org))
