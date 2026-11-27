"""platform operator authentication helpers (read-only role lookup, login audit)

Revision ID: e5b8c2d9f4a7
Revises: d4a7b1c8e3f6
Create Date: 2026-11-27 12:00:00.000000

Phase 4 of the organization lifecycle. The application role has NO privileges on `platform_operators`
or `platform_audit_events` (Phase 3), so the platform authentication code reaches them only through two
narrow SECURITY DEFINER functions (same conventions as `platform_set_organization_status`: fixed
`search_path`, owner = migration role, EXECUTE revoked from PUBLIC and granted to `ekip_app`):

* `platform_operator_role(user_id)` -- the operator's role (`admin` / `viewer`) if, and only if, the
  grant exists, is not revoked, and the user account is active and email-verified; otherwise NULL.
  Called on EVERY authenticated platform request, so a revoked operator loses access immediately.
* `platform_record_login(user_id, request_id)` -- appends a `platform.login` event to the private
  platform audit trail. It refuses (EK003) unless the caller is an active operator, so it cannot be used
  to write arbitrary audit rows. (Failed attempts are logged, not stored, so they cannot flood the table.)

No table, column or existing function changes. Downgrade drops the two functions.
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'e5b8c2d9f4a7'
down_revision: str | None = 'd4a7b1c8e3f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_FN = """
CREATE FUNCTION public.platform_operator_role(p_user_id uuid)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT o.role
      FROM public.platform_operators o
      JOIN public.users u ON u.id = o.user_id
     WHERE o.user_id = p_user_id
       AND o.revoked_at IS NULL
       AND u.is_active
       AND u.email_verified_at IS NOT NULL;
$$;
"""

_LOGIN_FN = """
CREATE FUNCTION public.platform_record_login(p_user_id uuid, p_request_id text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_user_id IS NULL OR public.platform_operator_role(p_user_id) IS NULL THEN
        RAISE EXCEPTION 'caller is not an active platform operator' USING ERRCODE = 'EK003';
    END IF;
    INSERT INTO public.platform_audit_events (actor_user_id, action, details, request_id)
    VALUES (p_user_id, 'platform.login', '{}'::jsonb, pg_catalog.left(p_request_id, 128));
END
$$;
"""

_APP_ROLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
        GRANT EXECUTE ON FUNCTION public.platform_operator_role(uuid) TO ekip_app;
        GRANT EXECUTE ON FUNCTION public.platform_record_login(uuid, text) TO ekip_app;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(_ROLE_FN)
    op.execute(_LOGIN_FN)
    op.execute("REVOKE ALL ON FUNCTION public.platform_operator_role(uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.platform_record_login(uuid, text) FROM PUBLIC")
    op.execute(_APP_ROLE)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.platform_record_login(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS public.platform_operator_role(uuid)")
