"""organization-scoped custom roles, RLS on roles/role_permissions, starter roles

Revision ID: e8c3a1f5b7d2
Revises: a8d4f2c6e1b9
Create Date: 2026-11-25 00:00:00.000000

Until now `roles` was one platform-wide catalog (unique `name`), shared by every
organization and deliberately outside row-level security ("genuinely global
catalogs", `c7d4e8f19a2b`). That made it impossible to let an organization
define its own roles: editing the single shared `admin` row would silently
change permissions for every tenant.

What this migration does
------------------------
1. `roles.organization_id` (nullable FK -> organizations, ON DELETE CASCADE).
   `NULL` marks a **built-in** role (today only `admin`, platform-wide,
   immutable); a value marks a **custom** role owned by one organization.
   Existing rows all become built-ins, so no existing assignment, invitation,
   access rule or project membership changes meaning.
2. Replaces the global `UNIQUE (name)` with two partial unique indexes on
   `lower(name)`: one across built-ins, one per organization.
3. Seeds, idempotently and *before* row-level security is switched on, every
   assignable permission code, the built-in `admin` role and its grants -- so a
   fresh database works even though the application role can no longer create
   built-in rows -- and backfills the three editable starter roles (`viewer`,
   `engineer`, `reviewer`) for every existing organization.
4. Row-level security on `roles` and `role_permissions` (ENABLE + FORCE):
     * SELECT: built-ins plus the current organization's own rows;
     * INSERT / UPDATE / DELETE: only rows owned by the current organization.
   So the application role (`ekip_app`) cannot modify or delete a built-in role
   regardless of what application code does, and cannot read or write another
   organization's roles. The policies use `NULLIF(current_setting(..), '')`
   so a connection whose setting was reset to the empty string behaves as
   "no tenant" (built-ins only) instead of raising a uuid-cast error.
5. A `SECURITY DEFINER` trigger on `user_roles`, `invitations`,
   `organization_access_rules` and `project_memberships` refuses to attach a
   role owned by organization A to anything belonging to organization B.
   Needed because foreign-key checks run with elevated privilege and ignore
   RLS: without it, a guessed role id from another tenant could be referenced.

Downgrade refuses to run while any custom role exists (it would otherwise have
to delete roles and, through `ON DELETE CASCADE`, their assignments). Remove
custom roles first.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e8c3a1f5b7d2'
down_revision: str | None = 'a8d4f2c6e1b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC = 'app.current_organization_id'
_TENANT = f"NULLIF(current_setting('{_GUC}', true), '')::uuid"

# Frozen copies of the catalog as of this migration (never import application
# code from a migration: it must keep working when that code changes).
_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ('incident:read', 'See incidents, their timelines and related details.'),
    ('incident:write', 'Report new incidents and update, resolve or annotate existing ones.'),
    ('postmortem:write', 'Draft and edit postmortems for incidents.'),
    ('postmortem:approve', 'Review and approve postmortems so they become part of the knowledge base.'),
    ('knowledge:review', 'Approve or reject knowledge proposed by EKIP before it is published.'),
    ('observability:read', 'See agent activity, usage and platform health views.'),
    ('audit:read', "Read the organization's audit trail of who changed what."),
    ('tenancy:manage', 'Full administration: users, roles, invitations, access rules, connectors, SSO and projects.'),
)
_ADMIN_NAME = 'admin'
_ADMIN_DESCRIPTION = "Full-access role granted to an organization's first user."
_STARTER_ROLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ('viewer', 'Read-only access to incidents and observability.',
     ('incident:read', 'observability:read')),
    ('engineer', 'Works incidents: report and update them and write postmortems.',
     ('incident:read', 'incident:write', 'postmortem:write', 'observability:read')),
    ('reviewer', 'Reviews and approves postmortems and proposed knowledge.',
     ('incident:read', 'observability:read', 'postmortem:approve', 'knowledge:review')),
)

_TRIGGER_TABLES: tuple[tuple[str, str, str], ...] = (
    # (table, role column, organization column)
    ('user_roles', 'role_id', 'organization_id'),
    ('invitations', 'grants_role_id', 'organization_id'),
    ('organization_access_rules', 'grants_role_id', 'organization_id'),
)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    # 1 + 2: schema ---------------------------------------------------------
    op.add_column(
        'roles',
        sa.Column(
            'organization_id', sa.UUID(), sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    # The original `UniqueConstraint('name')` was unnamed; find it by shape.
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT con.conname
                FROM pg_constraint con
                WHERE con.conrelid = 'public.roles'::regclass
                  AND con.contype = 'u'
                  AND array_length(con.conkey, 1) = 1
                  AND con.conkey[1] = (
                      SELECT attnum FROM pg_attribute
                      WHERE attrelid = 'public.roles'::regclass AND attname = 'name')
            LOOP
                EXECUTE format('ALTER TABLE public.roles DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END $$;
        """
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_roles_builtin_name ON roles (lower(name)) '
        'WHERE organization_id IS NULL'
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_roles_org_name ON roles (organization_id, lower(name)) '
        'WHERE organization_id IS NOT NULL'
    )

    # 3: seed catalog, built-in admin, starter roles (RLS is still off) ------
    perm_values = ', '.join(f'({_q(code)}, {_q(desc)})' for code, desc in _PERMISSIONS)
    op.execute(
        f'INSERT INTO permissions (code, description) VALUES {perm_values} '
        'ON CONFLICT (code) DO NOTHING'
    )
    for code, desc in _PERMISSIONS:
        op.execute(
            f'UPDATE permissions SET description = {_q(desc)} '
            f'WHERE code = {_q(code)} AND description IS NULL'
        )
    admin_codes = ', '.join(_q(code) for code, _ in _PERMISSIONS)
    op.execute(
        f"INSERT INTO roles (name, description) SELECT {_q(_ADMIN_NAME)}, {_q(_ADMIN_DESCRIPTION)} "
        f"WHERE NOT EXISTS (SELECT 1 FROM roles WHERE organization_id IS NULL "
        f"AND lower(name) = {_q(_ADMIN_NAME)})"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code IN (" + admin_codes + ") "
        f"WHERE r.organization_id IS NULL AND lower(r.name) = {_q(_ADMIN_NAME)} "
        "ON CONFLICT DO NOTHING"
    )
    starter_values = ', '.join(f'({_q(n)}, {_q(d)})' for n, d, _ in _STARTER_ROLES)
    op.execute(
        "INSERT INTO roles (organization_id, name, description) "
        "SELECT o.id, t.name, t.description FROM organizations o "
        f"CROSS JOIN (VALUES {starter_values}) AS t(name, description) "
        "WHERE NOT EXISTS (SELECT 1 FROM roles r WHERE lower(r.name) = lower(t.name) "
        "AND (r.organization_id IS NULL OR r.organization_id = o.id))"
    )
    for name, _desc, codes in _STARTER_ROLES:
        code_list = ', '.join(_q(c) for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r JOIN permissions p "
            f"ON p.code IN ({code_list}) "
            f"WHERE r.organization_id IS NOT NULL AND lower(r.name) = {_q(name)} "
            "ON CONFLICT DO NOTHING"
        )

    # 4: row-level security ---------------------------------------------------
    op.execute('ALTER TABLE roles ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE roles FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY roles_read ON roles FOR SELECT '
        f'USING (organization_id IS NULL OR organization_id = {_TENANT})'
    )
    op.execute(
        f'CREATE POLICY roles_insert ON roles FOR INSERT WITH CHECK (organization_id = {_TENANT})'
    )
    op.execute(
        f'CREATE POLICY roles_update ON roles FOR UPDATE '
        f'USING (organization_id = {_TENANT}) WITH CHECK (organization_id = {_TENANT})'
    )
    op.execute(f'CREATE POLICY roles_delete ON roles FOR DELETE USING (organization_id = {_TENANT})')

    op.execute('ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE role_permissions FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY role_permissions_read ON role_permissions FOR SELECT '
        'USING (role_id IN (SELECT id FROM roles))'  # roles' own SELECT policy applies
    )
    owned = f'role_id IN (SELECT id FROM roles WHERE organization_id = {_TENANT})'
    op.execute(
        f'CREATE POLICY role_permissions_insert ON role_permissions FOR INSERT WITH CHECK ({owned})'
    )
    op.execute(
        f'CREATE POLICY role_permissions_update ON role_permissions FOR UPDATE '
        f'USING ({owned}) WITH CHECK ({owned})'
    )
    op.execute(f'CREATE POLICY role_permissions_delete ON role_permissions FOR DELETE USING ({owned})')

    # 5: cross-organization attachment guard ----------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_role_org_scope() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            v_role uuid := (to_jsonb(NEW) ->> TG_ARGV[0])::uuid;
            v_org uuid := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid;
            v_role_org uuid;
        BEGIN
            SELECT organization_id INTO v_role_org FROM roles WHERE id = v_role;
            IF FOUND AND v_role_org IS NOT NULL AND v_role_org IS DISTINCT FROM v_org THEN
                RAISE EXCEPTION 'role % belongs to a different organization', v_role
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_project_membership_role_org_scope() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            v_role_org uuid;
            v_project_org uuid;
        BEGIN
            SELECT organization_id INTO v_role_org FROM roles WHERE id = NEW.role_id;
            IF FOUND AND v_role_org IS NOT NULL THEN
                SELECT organization_id INTO v_project_org FROM projects WHERE id = NEW.project_id;
                IF v_role_org IS DISTINCT FROM v_project_org THEN
                    RAISE EXCEPTION 'role % belongs to a different organization', NEW.role_id
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    for table, role_col, org_col in _TRIGGER_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table}_role_org_scope '
            f'BEFORE INSERT OR UPDATE OF {role_col}, {org_col} ON {table} '
            f"FOR EACH ROW EXECUTE FUNCTION enforce_role_org_scope('{role_col}', '{org_col}')"
        )
    op.execute(
        'CREATE TRIGGER trg_project_memberships_role_org_scope '
        'BEFORE INSERT OR UPDATE OF role_id, project_id ON project_memberships '
        'FOR EACH ROW EXECUTE FUNCTION enforce_project_membership_role_org_scope()'
    )


def downgrade() -> None:
    conn = op.get_bind()
    custom = conn.execute(sa.text('SELECT count(*) FROM roles WHERE organization_id IS NOT NULL')).scalar()
    if custom:
        raise RuntimeError(
            f'Refusing to downgrade: {custom} organization-owned custom role(s) exist and '
            'removing them would delete their assignments. Delete the custom roles first.'
        )
    op.execute('DROP TRIGGER IF EXISTS trg_project_memberships_role_org_scope ON project_memberships')
    for table, _role_col, _org_col in _TRIGGER_TABLES:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table}_role_org_scope ON {table}')
    op.execute('DROP FUNCTION IF EXISTS enforce_project_membership_role_org_scope()')
    op.execute('DROP FUNCTION IF EXISTS enforce_role_org_scope()')
    for policy in ('role_permissions_delete', 'role_permissions_update',
                   'role_permissions_insert', 'role_permissions_read'):
        op.execute(f'DROP POLICY IF EXISTS {policy} ON role_permissions')
    op.execute('ALTER TABLE role_permissions NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE role_permissions DISABLE ROW LEVEL SECURITY')
    for policy in ('roles_delete', 'roles_update', 'roles_insert', 'roles_read'):
        op.execute(f'DROP POLICY IF EXISTS {policy} ON roles')
    op.execute('ALTER TABLE roles NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE roles DISABLE ROW LEVEL SECURITY')
    op.execute('DROP INDEX IF EXISTS uq_roles_org_name')
    op.execute('DROP INDEX IF EXISTS uq_roles_builtin_name')
    op.execute('ALTER TABLE roles ADD CONSTRAINT roles_name_key UNIQUE (name)')
    op.drop_column('roles', 'organization_id')
