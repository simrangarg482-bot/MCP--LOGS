"""organization lifecycle: constrained status, session epoch, history, platform operators, protected transitions

Revision ID: d4a7b1c8e3f6
Revises: b2c5e8a1d4f7
Create Date: 2026-11-27 10:00:00.000000

Phase 3 of the organization lifecycle. Additive; **no existing row is rewritten and no
organization is promoted** -- every existing `onboarding` organization stays exactly as it is.

What it adds
------------
* `organizations.status` gets a database default (`onboarding`) and a CHECK constraint restricting it to
  `onboarding | active | suspended`. The constraint is added `NOT VALID` first, the existing data is then
  inspected, and only if every row is valid is it VALIDATED. **If any existing row holds another value the
  migration stops with an error naming the exact values and counts and changes nothing** -- it never
  converts an unknown status.
* `organizations.status_changed_at` (NULL = never changed, for every existing row),
  `organizations.status_message_public` (customer-facing text only; never an operator note) and
  `organizations.session_epoch integer NOT NULL DEFAULT 0` -- the organization-level session version.
  Access tokens carry the epoch they were issued under; it is bumped on every suspension AND every
  reactivation, so a token issued before a suspension can never become valid again. A counter (not a
  timestamp) so there is no clock comparison between the database and the API servers.
* A trigger on `organizations` makes the rules hold for EVERY writer, not just the application: only the
  approved transitions are accepted (`onboarding->active|suspended`, `active->suspended`,
  `suspended->active`); on any change involving `suspended` it bumps `session_epoch` and revokes every
  refresh token of that organization; it stamps `status_changed_at`.
* `platform_operators`, `organization_status_history`, `platform_audit_events` -- global tables (no
  organization scoping is possible or wanted) that the application role cannot touch at all
  (privileges revoked, row-level security enabled with no policy). History and audit rows are append-only.
* `platform_set_organization_status(...)` -- SECURITY DEFINER, the only approved way to change a status.
  It verifies the operator against `platform_operators`, locks the row, validates the transition, updates the
  status (the trigger does the epoch/refresh-token work), and writes the history row, the private platform
  audit event and a tenant-visible audit row in ONE transaction.
* `platform_is_active_operator(user_id)` -- a boolean the application can ask (the application role has no
  direct read access to `platform_operators`).

Privileges
----------
* `ekip_app` loses table-level UPDATE on `organizations` and keeps UPDATE only on `name`, `slug`,
  `updated_at` (INSERT and SELECT are unchanged, so signup still creates organizations). It cannot change
  `status` any more -- not even for its own organization.
* `ekip_app` gets EXECUTE on the two public functions only. `PUBLIC` gets none. The trigger functions are
  not executable by anyone but their owner.
* Function owner = the migration role, which already bypasses row-level security (the earlier
  SECURITY DEFINER functions in this repository rely on the same fact).

Error codes raised by the functions (SQLSTATE): EK001 invalid transition, EK002 already in that status,
EK003 caller is not an active platform operator, EK004 organization not found, EK005 invalid input,
EK006 append-only violation.

Downgrade removes everything above in dependency order and restores UPDATE on `organizations`. It does
NOT touch organization data; the lifecycle history and platform audit rows (new data) are dropped with
their tables.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4a7b1c8e3f6'
down_revision: str | None = 'b2c5e8a1d4f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = "status IN ('onboarding', 'active', 'suspended')"

_GUARD_EXISTING_DATA = """
DO $$
DECLARE
    v_bad text;
BEGIN
    SELECT string_agg(format('%L x %s', status, n), ', ' ORDER BY status)
      INTO v_bad
      FROM (
            SELECT status, count(*) AS n
              FROM public.organizations
             WHERE status IS NULL OR status NOT IN ('onboarding', 'active', 'suspended')
             GROUP BY status
           ) AS invalid;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'organizations.status holds values outside (onboarding, active, suspended): %. '
            'Nothing was changed. Decide what each of these organizations should be and correct them '
            'explicitly, then re-run the migration. This migration never converts an unknown status.', v_bad;
    END IF;
END
$$;
"""

_TRIGGER_FUNCTION = """
CREATE FUNCTION public.organizations_status_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.status = 'onboarding' AND NEW.status IN ('active', 'suspended'))
        OR (OLD.status = 'active' AND NEW.status = 'suspended')
        OR (OLD.status = 'suspended' AND NEW.status = 'active')
    ) THEN
        RAISE EXCEPTION 'invalid organization status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'EK001';
    END IF;
    NEW.status_changed_at := pg_catalog.now();
    IF OLD.status = 'suspended' OR NEW.status = 'suspended' THEN
        -- Every access token of this organization carries the epoch it was issued under: bumping it
        -- ends them all, and (because it is bumped again on reactivation) they stay ended.
        NEW.session_epoch := OLD.session_epoch + 1;
        UPDATE public.refresh_tokens
           SET revoked_at = pg_catalog.now()
         WHERE organization_id = OLD.id AND revoked_at IS NULL;
    END IF;
    IF NEW.status <> 'suspended' THEN
        NEW.status_message_public := NULL;
    END IF;
    RETURN NEW;
END
$$;
"""

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION public.platform_append_only()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- A cascade from deleting the ORGANIZATION (an erasure) runs this trigger one level deeper than a
    -- direct statement does; only that is allowed to remove history rows.
    IF TG_OP = 'DELETE' AND TG_TABLE_NAME = 'organization_status_history' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'EK006';
END
$$;
"""

_IS_OPERATOR_FUNCTION = """
CREATE FUNCTION public.platform_is_active_operator(p_user_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.platform_operators o
          JOIN public.users u ON u.id = o.user_id
         WHERE o.user_id = p_user_id AND o.revoked_at IS NULL AND u.is_active
    );
$$;
"""

_SET_STATUS_FUNCTION = """
CREATE FUNCTION public.platform_set_organization_status(
    p_operator_user_id uuid,
    p_organization_id uuid,
    p_new_status text,
    p_reason text,
    p_customer_message text DEFAULT NULL,
    p_internal_note text DEFAULT NULL,
    p_request_id text DEFAULT NULL
)
RETURNS TABLE (out_previous_status text, out_new_status text, out_history_id uuid, out_session_epoch integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_old text;
    v_history uuid;
    v_epoch integer;
    v_reason text := pg_catalog.btrim(coalesce(p_reason, ''));
    v_message text := nullif(pg_catalog.btrim(coalesce(p_customer_message, '')), '');
    v_note text := nullif(pg_catalog.btrim(coalesce(p_internal_note, '')), '');
    v_action text;
BEGIN
    -- 1. Authorization first: a caller that is not an active platform ADMIN learns nothing else.
    IF p_operator_user_id IS NULL OR NOT EXISTS (
        SELECT 1
          FROM public.platform_operators o
          JOIN public.users u ON u.id = o.user_id
         WHERE o.user_id = p_operator_user_id
           AND o.revoked_at IS NULL
           AND o.role = 'admin'
           AND u.is_active
    ) THEN
        RAISE EXCEPTION 'caller is not an active platform operator' USING ERRCODE = 'EK003';
    END IF;

    -- 2. Input
    IF p_new_status IS NULL OR p_new_status NOT IN ('active', 'suspended') THEN
        RAISE EXCEPTION 'the target status must be active or suspended' USING ERRCODE = 'EK001';
    END IF;
    IF pg_catalog.char_length(v_reason) < 1 OR pg_catalog.char_length(v_reason) > 1000 THEN
        RAISE EXCEPTION 'a reason of 1-1000 characters is required' USING ERRCODE = 'EK005';
    END IF;
    IF pg_catalog.char_length(coalesce(v_message, '')) > 1000
       OR pg_catalog.char_length(coalesce(v_note, '')) > 4000
       OR pg_catalog.char_length(coalesce(p_request_id, '')) > 128 THEN
        RAISE EXCEPTION 'customer message (1000), internal note (4000) or request id (128) too long'
            USING ERRCODE = 'EK005';
    END IF;

    -- 3. Serialize concurrent transitions of the same organization
    SELECT o.status INTO v_old FROM public.organizations o WHERE o.id = p_organization_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'organization not found' USING ERRCODE = 'EK004';
    END IF;

    -- 4. Transition rules. An already-completed transition is an ERROR, never silently accepted.
    IF v_old = p_new_status THEN
        RAISE EXCEPTION 'organization is already %', v_old USING ERRCODE = 'EK002';
    END IF;
    IF NOT (
        (v_old = 'onboarding' AND p_new_status IN ('active', 'suspended'))
        OR (v_old = 'active' AND p_new_status = 'suspended')
        OR (v_old = 'suspended' AND p_new_status = 'active')
    ) THEN
        RAISE EXCEPTION 'invalid organization status transition % -> %', v_old, p_new_status
            USING ERRCODE = 'EK001';
    END IF;

    -- 5. The change itself. The trigger stamps status_changed_at, bumps session_epoch and revokes the
    --    organization's refresh tokens when a suspension is involved.
    UPDATE public.organizations o
       SET status = p_new_status,
           status_message_public = CASE WHEN p_new_status = 'suspended' THEN v_message ELSE NULL END
     WHERE o.id = p_organization_id
    RETURNING o.session_epoch INTO v_epoch;

    -- 6. History (private), platform audit (private), tenant-visible audit (public facts only)
    INSERT INTO public.organization_status_history
        (organization_id, previous_status, new_status, reason, customer_message, internal_note,
         operator_user_id, request_id)
    VALUES
        (p_organization_id, v_old, p_new_status, v_reason, v_message, v_note, p_operator_user_id, p_request_id)
    RETURNING id INTO v_history;

    INSERT INTO public.platform_audit_events
        (actor_user_id, action, target_organization_id, details, request_id)
    VALUES
        (p_operator_user_id, 'organization.status_changed', p_organization_id,
         pg_catalog.jsonb_build_object(
             'from', v_old, 'to', p_new_status, 'history_id', v_history, 'session_epoch', v_epoch),
         p_request_id);

    v_action := CASE
        WHEN p_new_status = 'suspended' THEN 'organization.suspended'
        WHEN v_old = 'suspended' THEN 'organization.reactivated'
        ELSE 'organization.activated'
    END;
    INSERT INTO public.audit_logs (organization_id, actor, action, resource_type, resource_id, metadata)
    VALUES (
        p_organization_id, 'platform', v_action, 'organization', p_organization_id,
        pg_catalog.jsonb_build_object('from', v_old, 'to', p_new_status)
        || CASE WHEN p_new_status = 'suspended' AND v_message IS NOT NULL
                THEN pg_catalog.jsonb_build_object('message', v_message) ELSE '{}'::jsonb END
    );

    RETURN QUERY SELECT v_old, p_new_status, v_history, v_epoch;
END
$$;
"""

_SET_STATUS_SIGNATURE = (
    "public.platform_set_organization_status(uuid, uuid, text, text, text, text, text)"
)

_APP_ROLE_BLOCK = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
        {statements}
    END IF;
END
$$;
"""


def _for_app_role(*statements: str) -> None:
    op.execute(_APP_ROLE_BLOCK.format(statements="\n        ".join(s.rstrip(";") + ";" for s in statements)))


def upgrade() -> None:
    # --- 1. status: default + CHECK (NOT VALID -> inspect the data -> VALIDATE) -----------------------------
    op.execute("ALTER TABLE public.organizations ALTER COLUMN status SET DEFAULT 'onboarding'")
    op.execute(
        f"ALTER TABLE public.organizations ADD CONSTRAINT ck_organizations_status CHECK ({_STATUS_CHECK}) NOT VALID"
    )
    op.execute(_GUARD_EXISTING_DATA)  # stops here, naming the offending values, if any row is invalid
    op.execute("ALTER TABLE public.organizations VALIDATE CONSTRAINT ck_organizations_status")

    # --- 2. new organization columns ----------------------------------------------------------------------
    op.add_column('organizations', sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('organizations', sa.Column('status_message_public', sa.Text(), nullable=True))
    op.add_column(
        'organizations',
        sa.Column('session_epoch', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )

    # --- 3. platform tables ---------------------------------------------------------------------------------
    op.create_table(
        'platform_operators',
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('granted_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('user_id'),
        sa.CheckConstraint("role IN ('admin', 'viewer')", name='ck_platform_operators_role'),
    )
    op.create_table(
        'organization_status_history',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('previous_status', sa.Text(), nullable=False),
        sa.Column('new_status', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('customer_message', sa.Text(), nullable=True),
        sa.Column('internal_note', sa.Text(), nullable=True),
        sa.Column('operator_user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "previous_status IN ('onboarding', 'active', 'suspended') AND new_status IN ('active', 'suspended')",
            name='ck_organization_status_history_statuses',
        ),
    )
    op.create_index(
        'ix_organization_status_history_org_changed',
        'organization_status_history',
        ['organization_id', sa.text('changed_at DESC')],
    )
    op.create_table(
        'platform_audit_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor_user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('target_organization_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('details', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_platform_audit_events_org_occurred',
        'platform_audit_events',
        ['target_organization_id', sa.text('occurred_at DESC')],
    )

    # --- 4. functions and triggers --------------------------------------------------------------------------
    op.execute(_TRIGGER_FUNCTION)
    op.execute(
        "CREATE TRIGGER organizations_status_lifecycle BEFORE UPDATE OF status ON public.organizations "
        "FOR EACH ROW EXECUTE FUNCTION public.organizations_status_lifecycle()"
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER organization_status_history_append_only BEFORE UPDATE OR DELETE "
        "ON public.organization_status_history FOR EACH ROW EXECUTE FUNCTION public.platform_append_only()"
    )
    op.execute(
        "CREATE TRIGGER platform_audit_events_append_only BEFORE UPDATE OR DELETE "
        "ON public.platform_audit_events FOR EACH ROW EXECUTE FUNCTION public.platform_append_only()"
    )
    op.execute(_IS_OPERATOR_FUNCTION)
    op.execute(_SET_STATUS_FUNCTION)

    # --- 5. privileges --------------------------------------------------------------------------------------
    for table in ('platform_operators', 'organization_status_history', 'platform_audit_events'):
        # deny by default even if someone later grants a privilege: no policy exists for any role
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
    for function in (
        "public.organizations_status_lifecycle()",
        "public.platform_append_only()",
        "public.platform_is_active_operator(uuid)",
        _SET_STATUS_SIGNATURE,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
    _for_app_role(
        # the default privileges from b8f3d6a1c4e7 handed the application role full DML on every new table
        "REVOKE ALL ON public.platform_operators, public.organization_status_history, "
        "public.platform_audit_events FROM ekip_app",
        "REVOKE ALL ON FUNCTION public.organizations_status_lifecycle() FROM ekip_app",
        "REVOKE ALL ON FUNCTION public.platform_append_only() FROM ekip_app",
        # ... and the only status writer is the protected function: no UPDATE on lifecycle columns
        "REVOKE UPDATE ON public.organizations FROM ekip_app",
        "GRANT UPDATE (name, slug, updated_at) ON public.organizations TO ekip_app",
        "GRANT EXECUTE ON FUNCTION public.platform_is_active_operator(uuid) TO ekip_app",
        f"GRANT EXECUTE ON FUNCTION {_SET_STATUS_SIGNATURE} TO ekip_app",
    )


def downgrade() -> None:
    _for_app_role("GRANT UPDATE ON public.organizations TO ekip_app")
    op.execute("DROP TRIGGER IF EXISTS platform_audit_events_append_only ON public.platform_audit_events")
    op.execute("DROP TRIGGER IF EXISTS organization_status_history_append_only ON public.organization_status_history")
    op.execute("DROP TRIGGER IF EXISTS organizations_status_lifecycle ON public.organizations")
    op.execute(f"DROP FUNCTION IF EXISTS {_SET_STATUS_SIGNATURE}")
    op.execute("DROP FUNCTION IF EXISTS public.platform_is_active_operator(uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.platform_append_only()")
    op.execute("DROP FUNCTION IF EXISTS public.organizations_status_lifecycle()")
    op.drop_index('ix_platform_audit_events_org_occurred', table_name='platform_audit_events')
    op.drop_table('platform_audit_events')
    op.drop_index('ix_organization_status_history_org_changed', table_name='organization_status_history')
    op.drop_table('organization_status_history')
    op.drop_table('platform_operators')
    op.drop_column('organizations', 'session_epoch')
    op.drop_column('organizations', 'status_message_public')
    op.drop_column('organizations', 'status_changed_at')
    op.execute("ALTER TABLE public.organizations DROP CONSTRAINT IF EXISTS ck_organizations_status")
    op.execute("ALTER TABLE public.organizations ALTER COLUMN status DROP DEFAULT")
