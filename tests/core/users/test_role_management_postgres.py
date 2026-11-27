"""Role management against REAL PostgreSQL, as the real `ekip_app` role.

Tenant isolation, built-in-role immutability and the last-administrator
guarantee are database behaviour (row-level security, triggers, advisory
locks): they only mean something against a real, *migrated* database reached as
the real runtime role, so none of this is mocked.

Skipped unless both of these point at a LOCAL, disposable database on which
`alembic upgrade head` has been run (the same contract as
`tests/core/auth/test_email_verification_postgres.py`)::

    EKIP_TEST_POSTGRES_URL   superuser/owner URL -- only to inspect rows past RLS
                             and to clean up
    EKIP_TEST_APP_URL        the `ekip_app` URL (NOSUPERUSER NOBYPASSRLS); every
                             service call under test runs through THIS role

Every row a test creates carries a unique tag and is removed with targeted
DELETEs at teardown; nothing is truncated or dropped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationCreate, OrganizationCreate
from app.core.users import role_service
from app.core.users import service as users_service
from app.core.users.role_schemas import RoleCreate, RoleUpdate
from app.database.session import set_tenant_context

_SUPER_URL = os.environ.get("EKIP_TEST_POSTGRES_URL", "")
_APP_URL = os.environ.get("EKIP_TEST_APP_URL", "")


def _is_local(url: str) -> bool:
    return (urlsplit(url.replace("+asyncpg", "")).hostname or "") in {"localhost", "127.0.0.1", "::1"}


pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)


def _engine(url: str):
    return create_async_engine(
        url.replace("?sslmode=disable", ""), poolclass=NullPool, connect_args={"ssl": False}
    )


@pytest_asyncio.fixture
async def env():
    super_engine, app_engine = _engine(_SUPER_URL), _engine(_APP_URL)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:10]

    async with super_engine.connect() as conn:
        migrated = (
            await conn.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name='roles' "
                     "AND column_name='organization_id'")
            )
        ).scalar()
    if not migrated:
        pytest.skip("roles.organization_id missing: run `alembic upgrade head` on this database")

    class Env:
        pass

    e = Env()
    e.tag = tag

    async def run(org_id, fn):
        """One request-shaped unit of work as `ekip_app` under `org_id`'s tenant
        context: commit on success, roll back on any exception."""
        async with sessions() as session:
            try:
                if org_id is not None:
                    await set_tenant_context(session, org_id)
                result = await fn(session)
                await session.commit()
                return result
            except BaseException:
                await session.rollback()
                raise

    async def sup(sql, **params):
        async with super_engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return res.fetchall() if res.returns_rows else None

    e.run, e.sup = run, sup
    e.email = lambda name: f"rm-{tag}-{name}@example.invalid"
    e.slug = lambda name: f"rm-{tag}-{name}"
    yield e

    s = f"rm-{tag}-%"
    for sql in (
        "DELETE FROM audit_logs WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM project_memberships WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)",
        "DELETE FROM invitations WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)",
        "DELETE FROM projects WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM organizations WHERE slug LIKE :s",  # roles cascade
        "DELETE FROM users WHERE email LIKE :e",
    ):
        await sup(sql, e=f"rm-{tag}-%", s=s)
    await super_engine.dispose()
    await app_engine.dispose()


# ------------------------------------------------------------------ helpers


async def _make_org(env, name):
    org = await env.run(
        None,
        lambda s: tenancy_service.create_organization(
            s, OrganizationCreate(name=f"Org {name}", slug=env.slug(name))
        ),
    )
    return org.id


async def _make_user(env, org_id, name, role_ids):
    """A verified user holding `role_ids` in `org_id`, created the way signup does."""

    async def go(s):
        uid = await users_service.create_verified_user(
            s, email=env.email(name), display_name=name, password_hash=None, via="email_link"
        )
        for rid in role_ids:
            await users_service.assign_role(s, user_id=uid, organization_id=org_id, role_id=rid)
        return uid

    return await env.run(org_id, go)


async def _admin_role_id(env, org_id):
    return await env.run(org_id, users_service.ensure_admin_role)


async def _role_id(env, org_id, name):
    rows = await env.sup(
        "SELECT id FROM roles WHERE lower(name)=lower(:n) AND (organization_id=:o OR organization_id IS NULL) "
        "ORDER BY organization_id IS NULL LIMIT 1",
        n=name,
        o=org_id,
    )
    return rows[0][0]


async def _actor(env, org_id, user_id):
    return await env.run(org_id, lambda s: users_service.resolve_identity(s, user_id, org_id))


async def _setup_org(env, name="a"):
    """An organization with one admin (built-in admin role) and its starter roles."""
    org_id = await _make_org(env, name)
    admin_role = await _admin_role_id(env, org_id)
    admin_id = await _make_user(env, org_id, f"{name}-admin", [admin_role])
    return org_id, admin_role, admin_id


async def _audit_actions(env, org_id):
    rows = await env.sup("SELECT action FROM audit_logs WHERE organization_id=:o", o=org_id)
    return [r[0] for r in rows]


# ============================================================ the connection


@pytest.mark.asyncio
async def test_the_connection_under_test_really_is_the_rls_enforced_app_role(env) -> None:
    row = (
        await env.run(
            None,
            lambda s: s.execute(
                text("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ),
        )
    ).one()
    assert row[0] == "ekip_app" and row[1] is False and row[2] is False


# ===================================================== starter roles per org


@pytest.mark.asyncio
async def test_new_organization_gets_three_editable_starter_roles(env) -> None:
    org_id, _admin_role, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)

    roles = await env.run(org_id, lambda s: role_service.list_roles(s, actor, org_id))
    by_name = {r.name: r for r in roles}

    assert set(by_name) == {"admin", "viewer", "engineer", "reviewer"}
    assert by_name["admin"].is_builtin and not any(by_name[n].is_builtin for n in ("viewer", "engineer", "reviewer"))
    assert set(by_name["viewer"].permissions) == {"incident:read", "observability:read"}
    assert set(by_name["engineer"].permissions) == {
        "incident:read", "incident:write", "postmortem:write", "observability:read"
    }
    assert set(by_name["reviewer"].permissions) == {
        "incident:read", "observability:read", "postmortem:approve", "knowledge:review"
    }
    assert "system:graph_discovery" not in {p for r in roles for p in r.permissions}
    assert by_name["admin"].member_count == 1


@pytest.mark.asyncio
async def test_default_roles_creation_is_idempotent_and_keeps_edits(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)
    viewer = await _role_id(env, org_id, "viewer")
    await env.run(
        org_id,
        lambda s: role_service.update_role(
            s, actor, org_id, viewer, RoleUpdate(permissions=["incident:read"])
        ),
    )

    created = await env.run(org_id, lambda s: role_service.ensure_default_roles(s, org_id))

    assert created == []
    perms = await env.run(org_id, lambda s: role_service.get_role(s, actor, org_id, viewer))
    assert perms.permissions == ("incident:read",)


# ================================================== tenant isolation (RLS)


@pytest.mark.asyncio
async def test_an_organization_cannot_see_or_touch_another_organizations_roles(env) -> None:
    org_a, _ra, admin_a = await _setup_org(env, "a")
    org_b, _rb, admin_b = await _setup_org(env, "b")
    actor_a, actor_b = await _actor(env, org_a, admin_a), await _actor(env, org_b, admin_b)
    b_engineer = await _role_id(env, org_b, "engineer")
    a_engineer = await _role_id(env, org_a, "engineer")
    assert a_engineer != b_engineer

    # listing: A sees only built-ins + its own
    visible = await env.run(
        org_a,
        lambda s: s.execute(text("SELECT id, organization_id FROM roles")),
    )
    owners = {row[1] for row in visible.all()}
    assert owners <= {None, org_a}

    # service level: B's role is "not found" for A, for read / update / delete
    with pytest.raises(NotFoundError):
        await env.run(org_a, lambda s: role_service.get_role(s, actor_a, org_a, b_engineer))
    with pytest.raises(NotFoundError):
        await env.run(
            org_a, lambda s: role_service.update_role(s, actor_a, org_a, b_engineer, RoleUpdate(name="hacked"))
        )
    with pytest.raises(NotFoundError):
        await env.run(org_a, lambda s: role_service.delete_role(s, actor_a, org_a, b_engineer))
    # A's admin cannot manage B by putting B's id in the path
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(org_a, lambda s: role_service.list_roles(s, actor_a, org_b))
    assert exc.value.error_code == "users.cross_organization_denied"

    # database level, raw SQL as ekip_app in A's context
    async def raw_update(s):
        return (await s.execute(text("UPDATE roles SET name='pwned' WHERE id=:i"), {"i": b_engineer})).rowcount

    async def raw_delete(s):
        return (await s.execute(text("DELETE FROM roles WHERE id=:i"), {"i": b_engineer})).rowcount

    assert await env.run(org_a, raw_update) == 0
    assert await env.run(org_a, raw_delete) == 0
    with pytest.raises(DBAPIError):  # cannot create a role owned by another organization
        await env.run(
            org_a,
            lambda s: s.execute(
                text("INSERT INTO roles (organization_id, name) VALUES (:o, 'smuggled')"), {"o": org_b}
            ),
        )
    # and B is untouched
    still = await env.sup("SELECT name FROM roles WHERE id=:i", i=b_engineer)
    assert still[0][0] == "engineer"
    del actor_b


@pytest.mark.asyncio
async def test_role_permission_rows_are_isolated_too(env) -> None:
    org_a, _ra, _aa = await _setup_org(env, "a")
    org_b, _rb, _ab = await _setup_org(env, "b")
    b_viewer = await _role_id(env, org_b, "viewer")

    seen = await env.run(
        org_a,
        lambda s: s.execute(text("SELECT count(*) FROM role_permissions WHERE role_id=:r"), {"r": b_viewer}),
    )
    assert seen.scalar_one() == 0  # invisible to A

    async def raw_grant(s):
        await s.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT :r, id FROM permissions WHERE code='tenancy:manage'"
            ),
            {"r": b_viewer},
        )

    with pytest.raises(DBAPIError):
        await env.run(org_a, raw_grant)
    assert (
        await env.sup(
            "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
            "WHERE rp.role_id=:r AND p.code='tenancy:manage'",
            r=b_viewer,
        )
    )[0][0] == 0


@pytest.mark.asyncio
async def test_a_foreign_role_cannot_be_attached_to_another_organizations_members_invites_or_rules(env) -> None:
    org_a, admin_role, admin_a = await _setup_org(env, "a")
    org_b, _rb, _ab = await _setup_org(env, "b")
    b_engineer = await _role_id(env, org_b, "engineer")
    user_a = await _make_user(env, org_a, "plain", [admin_role])

    # the trigger fires even though the FK check itself ignores RLS
    async def attach_member(s):
        await s.execute(
            text("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)"),
            {"u": admin_a, "o": org_a, "r": b_engineer},
        )

    async def attach_invitation(s):
        await s.execute(
            text(
                "INSERT INTO invitations (organization_id,email,grants_role_id,invited_by,expires_at,status) "
                "VALUES (:o,'x@x.invalid',:r,:u,now()+interval '1 day','pending')"
            ),
            {"o": org_a, "r": b_engineer, "u": admin_a},
        )

    async def attach_rule(s):
        await s.execute(
            text(
                "INSERT INTO organization_access_rules (organization_id,rule_type,value,grants_role_id,is_active) "
                "VALUES (:o,'domain','x.invalid',:r,true)"
            ),
            {"o": org_a, "r": b_engineer},
        )

    for attach in (attach_member, attach_invitation, attach_rule):
        with pytest.raises(DBAPIError) as exc:
            await env.run(org_a, attach)
        assert "different organization" in str(exc.value)
    del user_a


@pytest.mark.asyncio
async def test_the_builtin_admin_role_is_immutable_for_the_app_role(env) -> None:
    org_id, admin_role, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)

    # service level
    for call in (
        lambda s: role_service.update_role(s, actor, org_id, admin_role, RoleUpdate(name="renamed")),
        lambda s: role_service.update_role(s, actor, org_id, admin_role, RoleUpdate(permissions=["audit:read"])),
        lambda s: role_service.delete_role(s, actor, org_id, admin_role),
    ):
        with pytest.raises(PermissionDeniedError) as exc:
            await env.run(org_id, call)
        assert exc.value.error_code == "role.builtin_immutable"

    # database level: even raw SQL cannot change it
    async def rename(s):
        return (await s.execute(text("UPDATE roles SET name='x' WHERE id=:i"), {"i": admin_role})).rowcount

    async def delete(s):
        return (await s.execute(text("DELETE FROM roles WHERE id=:i"), {"i": admin_role})).rowcount

    async def revoke(s):
        return (
            await s.execute(text("DELETE FROM role_permissions WHERE role_id=:i"), {"i": admin_role})
        ).rowcount

    async def grant_extra(s):
        await s.execute(
            text("INSERT INTO role_permissions (role_id, permission_id) SELECT :r, id FROM permissions "
                 "WHERE code='system:graph_discovery' ON CONFLICT DO NOTHING"),
            {"r": admin_role},
        )

    assert await env.run(org_id, rename) == 0
    assert await env.run(org_id, delete) == 0
    assert await env.run(org_id, revoke) == 0
    # (no row inserted == blocked or no such permission row; the check below is what matters)
    try:
        await env.run(org_id, grant_extra)
    except DBAPIError:
        pass
    row = (await env.sup("SELECT name FROM roles WHERE id=:i", i=admin_role))[0]
    assert row[0] == "admin"
    assert (
        await env.sup(
            "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
            "WHERE rp.role_id=:r",
            r=admin_role,
        )
    )[0][0] == 8
    assert (
        await env.sup(
            "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
            "WHERE rp.role_id=:r AND p.code='system:graph_discovery'",
            r=admin_role,
        )
    )[0][0] == 0


# ================================================================ role CRUD


@pytest.mark.asyncio
async def test_create_update_rename_and_delete_a_custom_role_with_audit(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)

    created = await env.run(
        org_id,
        lambda s: role_service.create_role(
            s, actor, org_id,
            RoleCreate(name="  Incident   Commander ", description="Runs incidents",
                       permissions=["incident:read", "incident:write", "incident:read"]),
        ),
    )
    assert created.name == "Incident Commander" and not created.is_builtin
    assert set(created.permissions) == {"incident:read", "incident:write"}

    updated = await env.run(
        org_id,
        lambda s: role_service.update_role(
            s, actor, org_id, created.id,
            RoleUpdate(name="IC", permissions=["incident:read", "postmortem:write"]),
        ),
    )
    assert updated.name == "IC" and set(updated.permissions) == {"incident:read", "postmortem:write"}

    await env.run(org_id, lambda s: role_service.delete_role(s, actor, org_id, created.id))
    gone = await env.sup("SELECT count(*) FROM roles WHERE id=:i", i=created.id)
    assert gone[0][0] == 0
    leftovers = await env.sup("SELECT count(*) FROM role_permissions WHERE role_id=:i", i=created.id)
    assert leftovers[0][0] == 0

    audit = await _audit_actions(env, org_id)
    assert audit.count("role.create") == 1 and audit.count("role.update") == 1 and audit.count("role.delete") == 1


@pytest.mark.asyncio
async def test_role_names_are_validated_and_unique_case_insensitively(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)

    for bad in ("", " ", "x", "-lead", "a" * 65, "bad/name", "<script>"):
        with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError
            RoleCreate(name=bad)

    for taken in ("engineer", "ENGINEER", "Admin"):  # starter role, case variant, built-in
        with pytest.raises(ConflictError) as exc:
            await env.run(
                org_id, lambda s, n=taken: role_service.create_role(s, actor, org_id, RoleCreate(name=n))
            )
        assert exc.value.error_code == "role.name_taken"

    # renaming onto an existing name is refused too; renaming to itself is fine
    engineer = await _role_id(env, org_id, "engineer")
    with pytest.raises(ConflictError):
        await env.run(
            org_id, lambda s: role_service.update_role(s, actor, org_id, engineer, RoleUpdate(name="Viewer"))
        )
    same = await env.run(
        org_id, lambda s: role_service.update_role(s, actor, org_id, engineer, RoleUpdate(name="engineer"))
    )
    assert same.name == "engineer"

    # the same name is fine in a different organization
    org_b, _rb, admin_b = await _setup_org(env, "b")
    actor_b = await _actor(env, org_b, admin_b)
    other = await env.run(
        org_b, lambda s: role_service.create_role(s, actor_b, org_b, RoleCreate(name="Responder"))
    )
    mine = await env.run(
        org_id, lambda s: role_service.create_role(s, actor, org_id, RoleCreate(name="responder"))
    )
    assert other.id != mine.id


@pytest.mark.asyncio
async def test_internal_and_unknown_permissions_are_not_assignable(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)

    for bad in ("system:graph_discovery", "root:everything"):
        with pytest.raises(ValidationError) as exc:
            await env.run(
                org_id,
                lambda s, b=bad: role_service.create_role(
                    s, actor, org_id, RoleCreate(name="Sneaky", permissions=[b])
                ),
            )
        assert exc.value.error_code == "role.unknown_permission"
    viewer = await _role_id(env, org_id, "viewer")
    with pytest.raises(ValidationError):
        await env.run(
            org_id,
            lambda s: role_service.update_role(
                s, actor, org_id, viewer, RoleUpdate(permissions=["system:graph_discovery"])
            ),
        )
    catalog = role_service.list_permission_catalog(actor, org_id)
    assert "system:graph_discovery" not in {p.code for p in catalog}
    assert {p.code for p in catalog} == set(users_service.repository.ADMIN_PERMISSION_CODES)


@pytest.mark.asyncio
async def test_authorization_every_role_operation_needs_tenancy_manage(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    viewer_role = await _role_id(env, org_id, "viewer")
    viewer_id = await _make_user(env, org_id, "viewer", [viewer_role])
    viewer = await _actor(env, org_id, viewer_id)
    engineer = await _role_id(env, org_id, "engineer")

    calls = (
        lambda s: role_service.list_roles(s, viewer, org_id),
        lambda s: role_service.get_role(s, viewer, org_id, engineer),
        lambda s: role_service.create_role(s, viewer, org_id, RoleCreate(name="Nope")),
        lambda s: role_service.update_role(s, viewer, org_id, engineer, RoleUpdate(name="Nope")),
        lambda s: role_service.delete_role(s, viewer, org_id, engineer),
        lambda s: role_service.add_member_role(s, viewer, org_id, viewer_id, engineer),
        lambda s: role_service.set_member_roles(s, viewer, org_id, viewer_id, [engineer]),
        lambda s: role_service.remove_member_role(s, viewer, org_id, viewer_id, viewer_role),
    )
    for call in calls:
        with pytest.raises(PermissionDeniedError) as exc:
            await env.run(org_id, call)
        assert exc.value.error_code == "permission_denied"
    # the catalog (no tenant data) is readable by any member
    assert role_service.list_permission_catalog(viewer, org_id)
    # nothing changed
    assert (await env.sup("SELECT name FROM roles WHERE id=:i", i=engineer))[0][0] == "engineer"
    del admin_id


@pytest.mark.asyncio
async def test_no_privilege_escalation_through_roles_invitations_or_assignment(env) -> None:
    org_id, admin_role, admin_id = await _setup_org(env)
    # a delegated administrator: may manage roles/users but holds only incident:read otherwise
    admin = await _actor(env, org_id, admin_id)
    deputy_role = await env.run(
        org_id,
        lambda s: role_service.create_role(
            s, admin, org_id, RoleCreate(name="Deputy", permissions=["tenancy:manage", "incident:read"])
        ),
    )
    deputy_id = await _make_user(env, org_id, "deputy", [deputy_role.id])
    deputy = await _actor(env, org_id, deputy_id)
    assert deputy.permissions == frozenset({"tenancy:manage", "incident:read"})

    # cannot create a role carrying a permission the deputy lacks
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(
            org_id,
            lambda s: role_service.create_role(
                s, deputy, org_id, RoleCreate(name="Bigger", permissions=["incident:write"])
            ),
        )
    assert exc.value.error_code == "role.permission_escalation"

    # cannot add such a permission to an existing role
    viewer = await _role_id(env, org_id, "viewer")
    with pytest.raises(PermissionDeniedError):
        await env.run(
            org_id,
            lambda s: role_service.update_role(
                s, deputy, org_id, viewer, RoleUpdate(permissions=["incident:read", "audit:read"])
            ),
        )
    # ...but may keep/remove permissions it already has
    ok = await env.run(
        org_id,
        lambda s: role_service.update_role(s, deputy, org_id, viewer, RoleUpdate(permissions=["incident:read"])),
    )
    assert ok.permissions == ("incident:read",)

    # cannot hand out the built-in admin role (or any role stronger than itself)
    target = await _make_user(env, org_id, "target", [viewer])
    engineer = await _role_id(env, org_id, "engineer")
    for role in (admin_role, engineer):
        with pytest.raises(PermissionDeniedError):
            await env.run(
                org_id, lambda s, r=role: role_service.add_member_role(s, deputy, org_id, target, r)
            )
        with pytest.raises(PermissionDeniedError):
            await env.run(
                org_id, lambda s, r=role: role_service.set_member_roles(s, deputy, org_id, target, [viewer, r])
            )

    # cannot invite into such a role either
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(
            org_id,
            lambda s: tenancy_service.create_invitation(
                s, deputy, org_id, InvitationCreate(email="x@example.invalid", grants_role="admin")
            ),
        )
    assert exc.value.error_code == "role.permission_escalation"
    del admin_role


@pytest.mark.asyncio
async def test_a_role_in_use_cannot_be_deleted(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)
    engineer = await _role_id(env, org_id, "engineer")
    await _make_user(env, org_id, "eng", [engineer])

    with pytest.raises(ConflictError) as exc:
        await env.run(org_id, lambda s: role_service.delete_role(s, actor, org_id, engineer))
    assert exc.value.error_code == "role.in_use" and exc.value.detail["usage"]["members"] == 1

    # an invitation referencing the role blocks deletion as well
    reviewer = await _role_id(env, org_id, "reviewer")
    await env.run(
        org_id,
        lambda s: tenancy_service.create_invitation(
            s, actor, org_id, InvitationCreate(email="inv@example.invalid", grants_role="reviewer")
        ),
    )
    with pytest.raises(ConflictError) as exc2:
        await env.run(org_id, lambda s: role_service.delete_role(s, actor, org_id, reviewer))
    assert exc2.value.detail["usage"]["invitations"] == 1
    assert (await env.sup("SELECT count(*) FROM roles WHERE id=:i", i=reviewer))[0][0] == 1


@pytest.mark.asyncio
async def test_invitation_role_names_resolve_inside_the_inviting_organization_only(env) -> None:
    org_a, _ra, admin_a = await _setup_org(env, "a")
    org_b, _rb, _ab = await _setup_org(env, "b")
    actor_a = await _actor(env, org_a, admin_a)
    # a role that exists only in B
    admin_b_actor = await _actor(env, org_b, await _make_user(env, org_b, "b2", [await _admin_role_id(env, org_b)]))
    await env.run(
        org_b, lambda s: role_service.create_role(s, admin_b_actor, org_b, RoleCreate(name="OnlyInB"))
    )

    with pytest.raises(ValidationError) as exc:
        await env.run(
            org_a,
            lambda s: tenancy_service.create_invitation(
                s, actor_a, org_a, InvitationCreate(email="z@example.invalid", grants_role="OnlyInB")
            ),
        )
    assert exc.value.error_code == "role.not_found"

    # a custom role of A resolves for A, and the built-in admin still resolves everywhere
    inv = await env.run(
        org_a,
        lambda s: tenancy_service.create_invitation(
            s, actor_a, org_a, InvitationCreate(email="ok@example.invalid", grants_role="Engineer")
        ),
    )
    assert inv.email == "ok@example.invalid"


# ==================================================== member role assignment


@pytest.mark.asyncio
async def test_add_remove_and_replace_member_roles_with_multiple_roles(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)
    viewer, engineer, reviewer = [await _role_id(env, org_id, n) for n in ("viewer", "engineer", "reviewer")]
    member = await _make_user(env, org_id, "m", [viewer])

    after_add = await env.run(org_id, lambda s: role_service.add_member_role(s, actor, org_id, member, engineer))
    assert set(after_add.roles) == {"viewer", "engineer"}
    # adding again is a no-op
    again = await env.run(org_id, lambda s: role_service.add_member_role(s, actor, org_id, member, engineer))
    assert again.role_ids == after_add.role_ids

    # permissions are the union, resolved fresh on the next request
    identity = await _actor(env, org_id, member)
    assert {"incident:write", "postmortem:write", "incident:read"} <= identity.permissions

    replaced = await env.run(
        org_id, lambda s: role_service.set_member_roles(s, actor, org_id, member, [reviewer, engineer])
    )
    assert set(replaced.roles) == {"reviewer", "engineer"}
    identity = await _actor(env, org_id, member)
    assert "knowledge:review" in identity.permissions

    after_remove = await env.run(
        org_id, lambda s: role_service.remove_member_role(s, actor, org_id, member, engineer)
    )
    assert after_remove.roles == ("reviewer",)
    identity = await _actor(env, org_id, member)
    assert "incident:write" not in identity.permissions  # demotion is immediate

    # the members listing exposes role ids for the editor
    members = await env.run(org_id, lambda s: users_service.list_organization_members(s, actor, org_id))
    mine = next(m for m in members if m.id == member)
    assert mine.role_ids == after_remove.role_ids and mine.roles == ("reviewer",)

    audit = await _audit_actions(env, org_id)
    assert {"member.role_add", "member.roles_set", "member.role_remove"} <= set(audit)


@pytest.mark.asyncio
async def test_a_member_always_keeps_at_least_one_role(env) -> None:
    org_id, _a, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)
    viewer = await _role_id(env, org_id, "viewer")
    member = await _make_user(env, org_id, "m", [viewer])

    with pytest.raises(ConflictError) as exc:
        await env.run(org_id, lambda s: role_service.remove_member_role(s, actor, org_id, member, viewer))
    assert exc.value.error_code == "member.last_role"
    with pytest.raises(ValueError):  # empty set rejected at the schema
        from app.core.users.role_schemas import MemberRolesSet

        MemberRolesSet(role_ids=[])


@pytest.mark.asyncio
async def test_member_operations_reject_strangers_foreign_members_and_foreign_roles(env) -> None:
    org_a, _ra, admin_a = await _setup_org(env, "a")
    org_b, rb_admin, admin_b = await _setup_org(env, "b")
    actor_a = await _actor(env, org_a, admin_a)
    b_engineer = await _role_id(env, org_b, "engineer")
    a_engineer = await _role_id(env, org_a, "engineer")

    # a member of B is "not found" from A
    with pytest.raises(NotFoundError) as exc:
        await env.run(org_a, lambda s: role_service.add_member_role(s, actor_a, org_a, admin_b, a_engineer))
    assert exc.value.error_code == "member.not_found"
    # a random id is the same
    with pytest.raises(NotFoundError):
        await env.run(
            org_a, lambda s: role_service.set_member_roles(s, actor_a, org_a, uuid.uuid4(), [a_engineer])
        )
    # B's role id cannot be assigned to A's member
    with pytest.raises(NotFoundError) as exc2:
        await env.run(org_a, lambda s: role_service.add_member_role(s, actor_a, org_a, admin_a, b_engineer))
    assert exc2.value.error_code == "role.not_found"
    with pytest.raises(NotFoundError):
        await env.run(
            org_a, lambda s: role_service.set_member_roles(s, actor_a, org_a, admin_a, [b_engineer])
        )
    # removing a role the member does not hold
    with pytest.raises(NotFoundError) as exc3:
        await env.run(
            org_a, lambda s: role_service.remove_member_role(s, actor_a, org_a, admin_a, a_engineer)
        )
    assert exc3.value.error_code == "member.role_not_held"
    # B's admin still has exactly their own role
    assert (await env.sup("SELECT count(*) FROM user_roles WHERE user_id=:u", u=admin_b))[0][0] == 1
    del rb_admin


# ========================================================= last administrator


@pytest.mark.asyncio
async def test_the_last_administrator_can_never_be_removed_or_demoted(env) -> None:
    org_id, admin_role, admin_id = await _setup_org(env)
    actor = await _actor(env, org_id, admin_id)
    viewer = await _role_id(env, org_id, "viewer")

    # replace the only admin's roles with a non-admin role
    with pytest.raises(ConflictError) as e1:
        await env.run(org_id, lambda s: role_service.set_member_roles(s, actor, org_id, admin_id, [viewer]))
    assert e1.value.error_code == "org.last_admin"

    # give them a second role then try to drop admin only
    await env.run(org_id, lambda s: role_service.add_member_role(s, actor, org_id, admin_id, viewer))
    with pytest.raises(ConflictError) as e2:
        await env.run(
            org_id, lambda s: role_service.remove_member_role(s, actor, org_id, admin_id, admin_role)
        )
    assert e2.value.error_code == "org.last_admin"

    # removing a non-admin role from them is fine
    ok = await env.run(
        org_id, lambda s: role_service.remove_member_role(s, actor, org_id, admin_id, viewer)
    )
    assert ok.roles == ("admin",)
    assert (
        await env.sup("SELECT count(*) FROM user_roles WHERE user_id=:u AND role_id=:r", u=admin_id, r=admin_role)
    )[0][0] == 1


@pytest.mark.asyncio
async def test_editing_a_role_cannot_strip_the_last_administrators_power(env) -> None:
    org_id, admin_role, admin_id = await _setup_org(env)
    boss = await _actor(env, org_id, admin_id)
    # the only person with tenancy:manage holds it through a CUSTOM role
    custom = await env.run(
        org_id,
        lambda s: role_service.create_role(
            s, boss, org_id, RoleCreate(name="Ops", permissions=["tenancy:manage", "incident:read"])
        ),
    )
    ops_user = await _make_user(env, org_id, "ops", [custom.id])
    # demote the original admin so Ops is the sole administrator
    await env.run(
        org_id,
        lambda s: role_service.set_member_roles(
            s, boss, org_id, admin_id, [custom.id]
        ),
    )
    ops = await _actor(env, org_id, ops_user)
    del admin_role

    with pytest.raises(ConflictError) as exc:
        await env.run(
            org_id,
            lambda s: role_service.update_role(
                s, ops, org_id, custom.id, RoleUpdate(permissions=["incident:read"])
            ),
        )
    assert exc.value.error_code == "org.last_admin"
    after = await env.run(org_id, lambda s: role_service.get_role(s, ops, org_id, custom.id))
    assert "tenancy:manage" in after.permissions  # the failed edit left nothing behind

    # editing unrelated parts of that role is fine
    renamed = await env.run(
        org_id, lambda s: role_service.update_role(s, ops, org_id, custom.id, RoleUpdate(name="Ops Lead"))
    )
    assert renamed.name == "Ops Lead"


@pytest.mark.asyncio
async def test_concurrent_demotions_cannot_remove_both_administrators(env) -> None:
    org_id, admin_role, admin1 = await _setup_org(env)
    admin2 = await _make_user(env, org_id, "second", [admin_role])
    viewer = await _role_id(env, org_id, "viewer")
    a1, a2 = await _actor(env, org_id, admin1), await _actor(env, org_id, admin2)

    results = await asyncio.gather(
        env.run(org_id, lambda s: role_service.set_member_roles(s, a1, org_id, admin2, [viewer])),
        env.run(org_id, lambda s: role_service.set_member_roles(s, a2, org_id, admin1, [viewer])),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, ConflictError)]
    assert len(failures) == 1, repr(results)
    assert failures[0].error_code == "org.last_admin"
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    admins = await env.sup(
        "SELECT count(DISTINCT ur.user_id) FROM user_roles ur WHERE ur.organization_id=:o AND ur.role_id=:r",
        o=org_id,
        r=admin_role,
    )
    assert admins[0][0] == 1
