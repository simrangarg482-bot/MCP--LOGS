"""platform read functions: list / detail / status history of organizations

Revision ID: f7c1d3a9b2e5
Revises: e5b8c2d9f4a7
Create Date: 2026-11-27 14:00:00.000000

Phase 5 of the organization lifecycle. The platform API must list organizations (with a member count that
needs the row-level-secured `user_roles` table) and show the status history, which the application role
cannot read at all. Rather than widening any privilege, each of these is a SECURITY DEFINER function that
(1) first verifies the caller is an active platform operator (`platform_operator_role`; any role may READ,
only `admin` may transition -- that stays inside `platform_set_organization_status`), and (2) returns exactly
the columns the platform console needs.

Same conventions as the earlier functions: fixed `search_path`, owner = migration role, EXECUTE revoked from
PUBLIC and granted to `ekip_app`. Read-only: nothing is written. No table, column or privilege changes.
Downgrade drops the three functions.

SQLSTATEs: EK003 not an active operator, EK004 organization not found, EK005 invalid input.
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'f7c1d3a9b2e5'
down_revision: str | None = 'e5b8c2d9f4a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = """
    IF p_operator_user_id IS NULL OR public.platform_operator_role(p_operator_user_id) IS NULL THEN
        RAISE EXCEPTION 'caller is not an active platform operator' USING ERRCODE = 'EK003';
    END IF;
"""

_LIST_FN = f"""
CREATE FUNCTION public.platform_list_organizations(
    p_operator_user_id uuid,
    p_search text DEFAULT NULL,
    p_status text DEFAULT NULL,
    p_limit integer DEFAULT 50,
    p_offset integer DEFAULT 0
)
RETURNS TABLE (
    out_id uuid, out_name text, out_slug text, out_status text, out_status_changed_at timestamptz,
    out_created_at timestamptz, out_member_count bigint, out_total bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_search text := nullif(pg_catalog.btrim(coalesce(p_search, '')), '');
    v_pattern text;
BEGIN
{_GUARD}
    IF p_status IS NOT NULL AND p_status NOT IN ('onboarding', 'active', 'suspended') THEN
        RAISE EXCEPTION 'unknown status filter' USING ERRCODE = 'EK005';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 200 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION 'invalid paging' USING ERRCODE = 'EK005';
    END IF;
    IF v_search IS NOT NULL THEN
        IF pg_catalog.char_length(v_search) > 200 THEN
            RAISE EXCEPTION 'search text too long' USING ERRCODE = 'EK005';
        END IF;
        v_pattern := '%' || pg_catalog.replace(pg_catalog.replace(pg_catalog.replace(v_search, '\\', '\\\\'), '%', '\\%'), '_', '\\_') || '%';
    END IF;
    RETURN QUERY
    SELECT o.id, o.name, o.slug, o.status, o.status_changed_at, o.created_at,
           (SELECT pg_catalog.count(DISTINCT ur.user_id) FROM public.user_roles ur WHERE ur.organization_id = o.id),
           pg_catalog.count(*) OVER ()
      FROM public.organizations o
     WHERE (p_status IS NULL OR o.status = p_status)
       AND (v_pattern IS NULL OR o.name ILIKE v_pattern OR o.slug ILIKE v_pattern)
     ORDER BY o.created_at DESC, o.id
     LIMIT p_limit OFFSET p_offset;
END
$$;
"""

_GET_FN = f"""
CREATE FUNCTION public.platform_get_organization(p_operator_user_id uuid, p_organization_id uuid)
RETURNS TABLE (
    out_id uuid, out_name text, out_slug text, out_status text, out_status_changed_at timestamptz,
    out_status_message_public text, out_created_at timestamptz, out_member_count bigint,
    out_session_epoch integer, out_history_count bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
{_GUARD}
    RETURN QUERY
    SELECT o.id, o.name, o.slug, o.status, o.status_changed_at, o.status_message_public, o.created_at,
           (SELECT pg_catalog.count(DISTINCT ur.user_id) FROM public.user_roles ur WHERE ur.organization_id = o.id),
           o.session_epoch,
           (SELECT pg_catalog.count(*) FROM public.organization_status_history h WHERE h.organization_id = o.id)
      FROM public.organizations o
     WHERE o.id = p_organization_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'organization not found' USING ERRCODE = 'EK004';
    END IF;
END
$$;
"""

_HISTORY_FN = f"""
CREATE FUNCTION public.platform_organization_status_history(
    p_operator_user_id uuid,
    p_organization_id uuid,
    p_limit integer DEFAULT 50,
    p_offset integer DEFAULT 0
)
RETURNS TABLE (
    out_id uuid, out_previous_status text, out_new_status text, out_reason text, out_customer_message text,
    out_internal_note text, out_operator_user_id uuid, out_operator_email text, out_changed_at timestamptz,
    out_request_id text, out_total bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
{_GUARD}
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 200 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION 'invalid paging' USING ERRCODE = 'EK005';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.organizations o WHERE o.id = p_organization_id) THEN
        RAISE EXCEPTION 'organization not found' USING ERRCODE = 'EK004';
    END IF;
    RETURN QUERY
    SELECT h.id, h.previous_status, h.new_status, h.reason, h.customer_message, h.internal_note,
           h.operator_user_id, u.email, h.changed_at, h.request_id, pg_catalog.count(*) OVER ()
      FROM public.organization_status_history h
      LEFT JOIN public.users u ON u.id = h.operator_user_id
     WHERE h.organization_id = p_organization_id
     ORDER BY h.changed_at DESC, h.id
     LIMIT p_limit OFFSET p_offset;
END
$$;
"""

_SIGNATURES = (
    "public.platform_list_organizations(uuid, text, text, integer, integer)",
    "public.platform_get_organization(uuid, uuid)",
    "public.platform_organization_status_history(uuid, uuid, integer, integer)",
)


def upgrade() -> None:
    op.execute(_LIST_FN)
    op.execute(_GET_FN)
    op.execute(_HISTORY_FN)
    for signature in _SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = "\n        ".join(f"GRANT EXECUTE ON FUNCTION {s} TO ekip_app;" for s in _SIGNATURES)
    op.execute(
        f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
        {grants}
    END IF;
END
$$;
"""
    )


def downgrade() -> None:
    for signature in _SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
