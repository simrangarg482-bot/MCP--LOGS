"""organization member states: organization-scoped deactivation of a member's access

Revision ID: a8e3c5d1f2b7
Revises: f7c1d3a9b2e5
Create Date: 2026-11-28 09:00:00.000000

Phase 1 of automated user offboarding. Today "membership" is simply "holds at least one `user_roles`
row", so there is no way to cut one person's access to ONE organization without deleting their data or
touching the global `users.is_active` flag (which would also lock them out of every other organization).
This adds the missing per-organization state.

`organization_member_states`
  * One row per (organization, user) that has EVER been deactivated. **A missing row means `active`**, so
    every existing membership keeps working and no backfill or data rewrite is needed.
  * `status` active | deactivated, `source` (admin now; scim is reserved for Phase 2), `reason`,
    `deactivated_at`, `member_epoch` and `updated_at`.
  * `member_epoch` is the member-level session version, the counterpart of `organizations.session_epoch`:
    access tokens carry the value they were issued under (claim `me`); it moves on every deactivation AND
    every reactivation, so a token issued before a deactivation can never become valid again.
  * `FORCE ROW LEVEL SECURITY` with the same `tenant_isolation` policy as the other tenant tables: one
    organization cannot read or change another's rows, even with a wrong filter.

A trigger makes the rules hold for EVERY writer, not only the application:
  * the epoch is computed by the database (0 on insert as active, 1 on insert as deactivated, +1 on every
    status change) and cannot be set or lowered by a statement;
  * `organization_id` and `user_id` cannot be changed;
  * a row cannot be deleted directly (deleting a deactivated row would reset the epoch to 0 and revive old
    tokens); only a cascade from deleting the organization or the user may remove it.

`member_deactivated_organization_ids(user_id)` is a narrow SECURITY DEFINER lookup (same pattern as
`list_user_organizations`): the login, organization-selection and switching paths run before any tenant
context exists, so row-level security would hide every row from them. It returns only organization ids,
fixed `search_path`, EXECUTE revoked from PUBLIC and granted to `ekip_app`.

Downgrade drops the function, the triggers and the table. Nothing else is touched.

SQLSTATEs: EK007 immutable column changed, EK008 direct delete refused.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a8e3c5d1f2b7'
down_revision: str | None = 'f7c1d3a9b2e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_FN = """
CREATE FUNCTION public.organization_member_state_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'deactivated' THEN
            NEW.member_epoch := 1;
            NEW.deactivated_at := pg_catalog.now();
        ELSE
            NEW.member_epoch := 0;
            NEW.deactivated_at := NULL;
        END IF;
        NEW.updated_at := pg_catalog.now();
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
            RAISE EXCEPTION 'organization_id and user_id of a member state cannot change' USING ERRCODE = 'EK007';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            -- every deactivation AND every reactivation moves the epoch: old tokens never revive
            NEW.member_epoch := OLD.member_epoch + 1;
            NEW.deactivated_at := CASE WHEN NEW.status = 'deactivated' THEN pg_catalog.now() ELSE NULL END;
        ELSE
            NEW.member_epoch := OLD.member_epoch;
            NEW.deactivated_at := OLD.deactivated_at;
        END IF;
        NEW.updated_at := pg_catalog.now();
        RETURN NEW;
    END IF;
    -- DELETE: only a cascade from deleting the organization or the user (one trigger level deeper) may
    -- remove a row; removing it directly would reset the epoch and could revive old access tokens.
    IF pg_catalog.pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'organization_member_states rows cannot be deleted directly' USING ERRCODE = 'EK008';
END
$$;
"""

_LOOKUP_FN = """
CREATE FUNCTION public.member_deactivated_organization_ids(p_user_id uuid)
RETURNS SETOF uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT s.organization_id
      FROM public.organization_member_states s
     WHERE s.user_id = p_user_id AND s.status = 'deactivated';
$$;
"""

_APP_ROLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
        GRANT EXECUTE ON FUNCTION public.member_deactivated_organization_ids(uuid) TO ekip_app;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        'organization_member_states',
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.Text(), server_default='active', nullable=False),
        sa.Column('source', sa.Text(), server_default='admin', nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('member_epoch', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('organization_id', 'user_id'),
        sa.CheckConstraint("status IN ('active', 'deactivated')", name='ck_member_states_status'),
        sa.CheckConstraint("source IN ('admin', 'scim')", name='ck_member_states_source'),
        sa.CheckConstraint("member_epoch >= 0", name='ck_member_states_epoch'),
    )
    op.create_index('ix_member_states_user', 'organization_member_states', ['user_id'])
    op.create_index('ix_member_states_org_status', 'organization_member_states', ['organization_id', 'status'])

    op.execute("ALTER TABLE public.organization_member_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.organization_member_states FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.organization_member_states "
        "USING (organization_id = current_setting('app.current_organization_id', true)::uuid)"
    )

    op.execute(_GUARD_FN)
    op.execute(
        "CREATE TRIGGER organization_member_state_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON public.organization_member_states FOR EACH ROW EXECUTE FUNCTION public.organization_member_state_guard()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.organization_member_state_guard() FROM PUBLIC")

    op.execute(_LOOKUP_FN)
    op.execute("REVOKE ALL ON FUNCTION public.member_deactivated_organization_ids(uuid) FROM PUBLIC")
    op.execute(_APP_ROLE)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.member_deactivated_organization_ids(uuid)")
    op.execute("DROP TRIGGER IF EXISTS organization_member_state_guard ON public.organization_member_states")
    op.drop_index('ix_member_states_org_status', table_name='organization_member_states')
    op.drop_index('ix_member_states_user', table_name='organization_member_states')
    op.drop_table('organization_member_states')
    op.execute("DROP FUNCTION IF EXISTS public.organization_member_state_guard()")
