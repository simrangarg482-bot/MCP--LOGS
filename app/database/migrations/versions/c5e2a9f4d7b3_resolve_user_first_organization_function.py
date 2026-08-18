"""resolve_user_first_organization: SECURITY DEFINER bootstrap for password login

Revision ID: c5e2a9f4d7b3
Revises: b8f3d6a1c4e7
Create Date: 2026-08-18 00:00:00.000001

Closes the same "chicken-and-egg" gap `d2e5f8a3c1b6`'s four functions
already close, one call site over: `core.auth.service.login_with_password`
calls `core.users.service.resolve_organization_for_login` ->
`core.users.repository.get_first_organization_id` to discover which
organization a password-authenticated user should log into, *before* any
`Identity`/org context exists yet for `app.database.session.
set_tenant_context` to be called with -- the organization id it needs is
itself only reachable via a `user_roles` row RLS would otherwise hide.
`user_roles` is one of migration `c7d4e8f19a2b`'s `_DIRECT_TABLES` (`ENABLE
ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`), so this is a real,
confirmed gap, not a hypothetical one: under the current `neondb_owner`
connection (`BYPASSRLS`) `get_first_organization_id`'s plain `SELECT ...
FROM user_roles WHERE user_id = :user_id LIMIT 1` already works, silently
riding on the same blanket bypass this whole migration sequence exists to
retire -- but the moment the application actually connects as `ekip_app`
(`b8f3d6a1c4e7`, immediately before this migration) instead, that same query
would return zero rows for every password-login attempt, since no tenant
context can exist yet at that point in the flow. Password login would break
completely, silently, for every user, the day `DATABASE_URL` is switched --
exactly the kind of regression a "the SQL looks right" review would miss and
only a real end-to-end login attempt under the narrower role would catch.

PROVENANCE: a function with this exact name and signature already exists in
the project's shared Neon database today, created directly against it (not
through any migration, on `main` or the abandoned branch -- see migration
`90ff736ced55`'s own docstring, which found it via schema introspection and
deliberately did not drop it, since it was "real, further-along progress" on
this exact problem, not resolved scaffolding). Its actual defining SQL was
never captured anywhere this migration could read verbatim; the body below
is authored fresh, but to the same specification `90ff736ced55` already
inferred from its `ekip_app`-targeted grant and name -- deliberately matching
`get_first_organization_id`'s exact current semantics ("one organization_id
this user_id holds a role in, or none"), the same query shape as
`d2e5f8a3c1b6`'s `resolve_connector_config_organization`/
`resolve_document_organization`/`resolve_refresh_token_organization`, and
the same `SET search_path = public` hardening those three already establish
as this codebase's standard for every `SECURITY DEFINER` function (closes
the well-known footgun where an unpinned search_path lets a hostile
caller-session search_path redirect what a superuser-privileged function
actually reads).

Deliberately NOT wired into `core.users.repository.get_first_organization_id`
in this same change -- that Python-side switch is `core.auth.service`'s
concern (it is the one caller that actually needs the pre-tenant-context
bypass; `get_first_organization_id` has no other caller today, so changing
it here would be silently coupling a database migration to an application
code change with no test coverage added alongside it in the same commit).
See the paired `app/core/users/repository.py` change in this same batch of
work for that half.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5e2a9f4d7b3'
down_revision: str | None = 'b8f3d6a1c4e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION resolve_user_first_organization(user_id_arg uuid)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT organization_id FROM user_roles WHERE user_id = user_id_arg LIMIT 1;
        $$;
        """
    )


def downgrade() -> None:
    op.execute('DROP FUNCTION IF EXISTS resolve_user_first_organization(uuid)')
