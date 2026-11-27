"""HTTP-level tests for `app.api.routers.roles`.

Same `TestClient` + `dependency_overrides` style as `test_users_router.py`. The
authorization paths (`tenancy:manage`, cross-organization) run the REAL service
code, which refuses before any database access, so the session is a harmless
`None`; happy paths stub the service call and assert the router passes the
caller's identity and the path organization through untouched. Behaviour that
needs a database (RLS, last administrator, ...) is covered against real
Postgres in `tests/core/users/test_role_management_postgres.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import roles as roles_router
from app.core.exceptions import ConflictError, NotFoundError
from app.core.users.role_schemas import MemberRoles, Role
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _identity(organization_id: uuid.UUID, *permissions: str) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset(permissions),
    )


@pytest.fixture()
def org_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def make_client():
    async def _fake_session():
        yield None

    def build(actor: Identity) -> TestClient:
        api_main.app.dependency_overrides[get_current_identity] = lambda: actor
        api_main.app.dependency_overrides[get_db_session] = _fake_session
        return TestClient(api_main.app, raise_server_exceptions=False)

    yield build
    api_main.app.dependency_overrides.clear()


def _role(**overrides: object) -> Role:
    base = dict(
        id=uuid.uuid4(),
        name="Responder",
        description=None,
        is_builtin=False,
        permissions=("incident:read",),
        member_count=0,
    )
    base.update(overrides)
    return Role(**base)  # type: ignore[arg-type]


# --- authorization ---------------------------------------------------------------------


def test_every_role_management_endpoint_requires_tenancy_manage(make_client, org_id) -> None:
    client = make_client(_identity(org_id, "incident:read"))  # a plain member
    role_id, user_id = uuid.uuid4(), uuid.uuid4()
    base = f"/organizations/{org_id}"
    calls = [
        ("GET", f"{base}/roles", None),
        ("POST", f"{base}/roles", {"name": "Nope", "permissions": []}),
        ("GET", f"{base}/roles/{role_id}", None),
        ("PATCH", f"{base}/roles/{role_id}", {"name": "Nope"}),
        ("DELETE", f"{base}/roles/{role_id}", None),
        ("PUT", f"{base}/members/{user_id}/roles", {"role_ids": [str(role_id)]}),
        ("POST", f"{base}/members/{user_id}/roles", {"role_id": str(role_id)}),
        ("DELETE", f"{base}/members/{user_id}/roles/{role_id}", None),
    ]
    for method, url, body in calls:
        response = client.request(method, url, json=body)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error_code"] == "permission_denied"


def test_another_organizations_path_is_refused_even_for_an_admin(make_client, org_id) -> None:
    client = make_client(_identity(org_id, "tenancy:manage"))
    other = uuid.uuid4()
    for method, url, body in (
        ("GET", f"/organizations/{other}/roles", None),
        ("POST", f"/organizations/{other}/roles", {"name": "Sneaky", "permissions": []}),
        ("GET", f"/organizations/{other}/permissions", None),
        (
            "PUT",
            f"/organizations/{other}/members/{uuid.uuid4()}/roles",
            {"role_ids": [str(uuid.uuid4())]},
        ),
    ):
        response = client.request(method, url, json=body)
        assert response.status_code == 403, (method, url)
        assert response.json()["error_code"] == "users.cross_organization_denied"


def test_the_permission_catalog_is_readable_by_any_member_and_hides_internal_codes(
    make_client, org_id
) -> None:
    client = make_client(_identity(org_id))  # no permissions at all
    response = client.get(f"/organizations/{org_id}/permissions")
    assert response.status_code == 200
    codes = {p["code"] for p in response.json()}
    assert "tenancy:manage" in codes and "incident:read" in codes
    assert "system:graph_discovery" not in codes
    assert all(p["label"] and p["description"] and p["group"] for p in response.json())


# --- validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": " "},
        {"name": "x"},
        {"name": "-bad"},
        {"name": "a" * 65},
        {"name": "ok name", "permissions": "incident:read"},
        {"name": "ok", "unknown": 1},
        {},
    ],
)
def test_create_role_rejects_invalid_bodies(make_client, org_id, body) -> None:
    client = make_client(_identity(org_id, "tenancy:manage"))
    assert client.post(f"/organizations/{org_id}/roles", json=body).status_code == 422


def test_member_roles_body_must_contain_at_least_one_role(make_client, org_id) -> None:
    client = make_client(_identity(org_id, "tenancy:manage"))
    url = f"/organizations/{org_id}/members/{uuid.uuid4()}/roles"
    assert client.put(url, json={"role_ids": []}).status_code == 422
    assert client.put(url, json={"role_ids": ["not-a-uuid"]}).status_code == 422


# --- happy paths and error mapping (service stubbed) -----------------------------------------


def test_create_role_passes_actor_and_path_organization_to_the_service(
    make_client, org_id, monkeypatch
) -> None:
    actor = _identity(org_id, "tenancy:manage")
    client = make_client(actor)
    seen: dict[str, object] = {}

    async def fake_create_role(session, who, organization_id, data):
        seen.update(who=who, organization_id=organization_id, data=data)
        return _role(name=data.name)

    monkeypatch.setattr(roles_router.role_service, "create_role", fake_create_role)
    response = client.post(
        f"/organizations/{org_id}/roles",
        json={"name": "  Incident   Lead ", "description": "  d ", "permissions": ["incident:read"]},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Incident Lead" and response.json()["is_builtin"] is False
    assert seen["who"] is actor and seen["organization_id"] == org_id
    assert seen["data"].description == "d"  # trimmed


def test_delete_returns_204_and_service_errors_map_to_http_statuses(
    make_client, org_id, monkeypatch
) -> None:
    client = make_client(_identity(org_id, "tenancy:manage"))
    role_id = uuid.uuid4()
    outcome: dict[str, Exception | None] = {"error": None}

    async def fake_delete(session, actor, organization_id, rid):
        if outcome["error"] is not None:
            raise outcome["error"]

    monkeypatch.setattr(roles_router.role_service, "delete_role", fake_delete)
    url = f"/organizations/{org_id}/roles/{role_id}"

    assert client.delete(url).status_code == 204
    outcome["error"] = ConflictError("in use", error_code="role.in_use")
    response = client.delete(url)
    assert response.status_code == 409 and response.json()["error_code"] == "role.in_use"
    outcome["error"] = NotFoundError("nope", error_code="role.not_found")
    assert client.delete(url).status_code == 404


def test_member_role_endpoints_return_the_resulting_role_set(
    make_client, org_id, monkeypatch
) -> None:
    client = make_client(_identity(org_id, "tenancy:manage"))
    user_id, role_id = uuid.uuid4(), uuid.uuid4()
    result = MemberRoles(user_id=user_id, role_ids=(role_id,), roles=("engineer",))

    async def fake_add(session, actor, organization_id, uid, rid):
        assert (uid, rid) == (user_id, role_id)
        return result

    async def fake_set(session, actor, organization_id, uid, rids):
        assert uid == user_id and list(rids) == [role_id]
        return result

    async def fake_remove(session, actor, organization_id, uid, rid):
        return result

    monkeypatch.setattr(roles_router.role_service, "add_member_role", fake_add)
    monkeypatch.setattr(roles_router.role_service, "set_member_roles", fake_set)
    monkeypatch.setattr(roles_router.role_service, "remove_member_role", fake_remove)
    base = f"/organizations/{org_id}/members/{user_id}/roles"

    assert client.post(base, json={"role_id": str(role_id)}).status_code == 201
    assert client.put(base, json={"role_ids": [str(role_id)]}).json()["roles"] == ["engineer"]
    assert client.delete(f"{base}/{role_id}").status_code == 200


def test_the_routes_are_published_in_the_openapi_schema() -> None:
    paths = api_main.app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/organizations/{organization_id}/roles"])
    assert {"get", "patch", "delete"} <= set(
        paths["/organizations/{organization_id}/roles/{role_id}"]
    )
    assert {"put", "post"} <= set(paths["/organizations/{organization_id}/members/{user_id}/roles"])
    assert "delete" in paths["/organizations/{organization_id}/members/{user_id}/roles/{role_id}"]
    assert "get" in paths["/organizations/{organization_id}/permissions"]
