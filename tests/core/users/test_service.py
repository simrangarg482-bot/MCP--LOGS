"""Tests for `app.core.users.service.resolve_identity`'s Milestone 10 RLS
fix, and for the project-scoped permission pipeline (`Identity.
project_permissions` population + `require_project_permission`) added in
the integration-gaps pass -- not a full test suite for `core.users.service`
(no test infrastructure for that module existed before the first of these
additions).

The RLS-ordering test below is the one genuinely critical bug found during
Milestone 10's rollout: `resolve_identity` is called *before*
`app.api.deps.get_current_identity`/`app.mcp.dispatch.run_mcp_tool` get a
chance to call `set_tenant_context` (both call it only after
`resolve_identity` already returned an `Identity`), but `resolve_identity`
itself queries `user_roles` (RLS-protected) via `get_role_names`/
`get_permission_codes`. Without setting the GUC first, *every* request would
have silently resolved to an `Identity` with empty roles/permissions --
failing every `authorize()` check closed, not raising an error -- which
would have been extremely difficult to notice or debug after the fact.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import PermissionDeniedError
from app.core.users import service as users_service
from app.shared.schemas import ActorKind, Identity


class _FakeUserRow:
    def __init__(self, *, user_id: uuid.UUID, is_active: bool = True) -> None:
        self.id = user_id
        self.is_active = is_active
        self.display_name = "Test User"


def _patch_resolve_identity_dependencies(
    monkeypatch,
    *,
    user_row: _FakeUserRow,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role_names: list[str] | None = None,
    permission_codes: set[str] | None = None,
    project_permission_map: dict[uuid.UUID, frozenset[str]] | None = None,
    call_order: list[str] | None = None,
) -> None:
    """Shared plumbing for every test below that calls `resolve_identity` --
    patches all four of its dependencies (`set_tenant_context`, and the
    three `repository` lookups) at once, so adding `get_project_permission_map`
    (the newest of the four) doesn't require repeating this every time.
    """
    order = call_order if call_order is not None else []

    async def fake_set_tenant_context(session, org_id) -> None:
        order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_by_id(session, user_id_arg):
        order.append("get_by_id")
        assert user_id_arg == user_id
        return user_row

    async def fake_get_role_names(session, user_id_arg, org_id):
        order.append("get_role_names")
        return role_names or []

    async def fake_get_permission_codes(session, user_id_arg, org_id):
        order.append("get_permission_codes")
        return permission_codes or set()

    async def fake_get_project_permission_map(session, user_id_arg, org_id):
        order.append("get_project_permission_map")
        return project_permission_map or {}

    monkeypatch.setattr(users_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(users_service.repository, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(users_service.repository, "get_role_names", fake_get_role_names)
    monkeypatch.setattr(users_service.repository, "get_permission_codes", fake_get_permission_codes)
    monkeypatch.setattr(
        users_service.repository, "get_project_permission_map", fake_get_project_permission_map
    )


@pytest.mark.asyncio
async def test_resolve_identity_sets_tenant_context_before_querying_roles(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user_row = _FakeUserRow(user_id=user_id)
    call_order: list[str] = []

    _patch_resolve_identity_dependencies(
        monkeypatch,
        user_row=user_row,
        organization_id=organization_id,
        user_id=user_id,
        role_names=["responder"],
        permission_codes={"incidents:read"},
        call_order=call_order,
    )

    identity = await users_service.resolve_identity(None, user_id, organization_id)

    assert identity.permissions == frozenset({"incidents:read"})
    # set_tenant_context must be the very first thing this function does --
    # before even the user-existence check -- since get_role_names/
    # get_permission_codes both depend on it having already run.
    assert call_order[0] == "set_tenant_context"
    assert call_order.index("set_tenant_context") < call_order.index("get_role_names")
    assert call_order.index("set_tenant_context") < call_order.index("get_permission_codes")


@pytest.mark.asyncio
async def test_resolve_identity_populates_project_permissions(monkeypatch) -> None:
    """The integration-gaps pass's core deliverable: `resolve_identity` must
    actually populate `Identity.project_permissions` from
    `repository.get_project_permission_map`, not leave it at its empty-dict
    default the way it did before this pipeline was wired up.
    """
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    user_row = _FakeUserRow(user_id=user_id)

    _patch_resolve_identity_dependencies(
        monkeypatch,
        user_row=user_row,
        organization_id=organization_id,
        user_id=user_id,
        permission_codes={"incident:write"},
        project_permission_map={project_id: frozenset({"knowledge:review"})},
    )

    identity = await users_service.resolve_identity(None, user_id, organization_id)

    assert identity.project_permissions == {project_id: frozenset({"knowledge:review"})}
    # A permission granted only at the project level must not leak into the
    # org-level check for a *different* project/no project at all.
    assert identity.has_permission("knowledge:review") is False
    assert identity.has_permission("knowledge:review", project_id=project_id) is True
    # Org-level permissions remain reachable exactly as before, for a project
    # with no override.
    assert identity.has_permission("incident:write", project_id=uuid.uuid4()) is True


@pytest.mark.asyncio
async def test_resolve_identity_defaults_to_empty_project_permissions(monkeypatch) -> None:
    """A user with no `project_memberships` row anywhere resolves to an
    empty `project_permissions` dict, not an error -- every project-scoped
    check for them simply falls back to their org-level permissions,
    identical to `Identity`'s pre-pipeline default behavior.
    """
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user_row = _FakeUserRow(user_id=user_id)

    _patch_resolve_identity_dependencies(
        monkeypatch,
        user_row=user_row,
        organization_id=organization_id,
        user_id=user_id,
        permission_codes={"incident:write"},
    )

    identity = await users_service.resolve_identity(None, user_id, organization_id)

    assert identity.project_permissions == {}
    assert identity.has_permission("incident:write", project_id=uuid.uuid4()) is True


# --- require_project_permission ----------------------------------------------


def _identity_with_project_permissions(
    organization_id: uuid.UUID,
    project_permissions: dict[uuid.UUID, frozenset[str]],
    *,
    permissions: frozenset[str] = frozenset(),
) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=permissions,
        project_permissions=project_permissions,
    )


def test_require_project_permission_succeeds_for_granted_project() -> None:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = _identity_with_project_permissions(
        organization_id, {project_id: frozenset({"incident:write"})}
    )

    users_service.require_project_permission(actor, project_id, "incident:write")  # no raise


def test_require_project_permission_denies_for_a_different_project() -> None:
    organization_id = uuid.uuid4()
    granted_project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    actor = _identity_with_project_permissions(
        organization_id, {granted_project_id: frozenset({"incident:write"})}
    )

    with pytest.raises(PermissionDeniedError):
        users_service.require_project_permission(actor, other_project_id, "incident:write")


def test_require_project_permission_falls_back_to_org_level_when_no_override() -> None:
    """A project with no `project_permissions` entry at all still honors the
    caller's org-level permission set -- `require_project_permission` is a
    call-site-clarity wrapper, not a stricter/different enforcement path.
    """
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = _identity_with_project_permissions(
        organization_id, {}, permissions=frozenset({"incident:write"})
    )

    users_service.require_project_permission(actor, project_id, "incident:write")  # no raise
