"""milestone 10: narrow RLS-bypass functions for pre-Identity bare-PK lookups

Revision ID: d2e5f8a3c1b6
Revises: c7d4e8f19a2b
Create Date: 2026-08-05 00:00:00.000001

Closes the "chicken-and-egg" gap the previous migration's RLS policies
introduce: a handful of code paths must discover a row's own
`organization_id` *before* `app.database.session.set_tenant_context` can be
called (since that's the only thing that makes the row visible under RLS in
the first place). Three call sites need this, all identified and traced
during Milestone 10's RLS rollout:

1. `app.ingestion.service._execute_ingestion_job`'s very first read --
   `connector_configs` by bare `connector_config_id` -- runs in a worker
   process with no `Identity`/org context yet; the organization_id it needs
   in order to call `set_tenant_context` is itself a column on the exact row
   RLS would otherwise hide.
2. `app.ingestion.service.reindex`'s very first read -- `documents` by bare
   `document_id` -- the identical shape of problem, one table over.
3. `app.ingestion.workers.tasks.scheduled_reconciliation`'s periodic scan --
   deliberately cross-tenant by design (it must enumerate *every*
   organization's active connectors, the same "no single org context to set"
   situation `core.tenancy.repository.list_organizations` is in for the
   Knowledge Gap Agent's scan, except `connector_configs`, unlike
   `organizations`, *is* RLS-protected).
4. `app.core.auth.service.refresh`/`logout` -- both start from a bare,
   client-presented refresh-token hash with no `Identity`/org context yet
   (that is what this call is for: re-establishing one), and
   `refresh_tokens` is RLS-protected.

Each function below is a `SECURITY DEFINER` function -- runs with the
privileges of whichever role owns it (the migration-running role, i.e. the
table owner), bypassing RLS -- deliberately narrowed to return the absolute
minimum needed for its one caller, rather than granting the application's
own connection role a blanket `BYPASSRLS` attribute. This follows
PROJECT_PLAN.md section 12.8's "least privilege" value: the bypass surface
is exactly "which org does this one row belong to" or "which ids are
currently active," never "read this row's full contents," and never
generalized into a reusable trapdoor.

`SET search_path = public` on each is deliberate hardening (a well-known
`SECURITY DEFINER` footgun: without a fixed search_path, a caller in a
session with a hostile `search_path` could get this function to resolve
`connector_configs`/`documents` to a same-named object it created in a
schema earlier in the path, effectively re-pointing what a superuser-executed
function reads/writes -- pinning `search_path` here closes that regardless
of whatever the calling session's own `search_path` is set to).

After calling `resolve_connector_config_organization`/
`resolve_document_organization`, the caller still calls `set_tenant_context`
and then the *normal*, RLS-scoped `get_connector_config`/`get_document_by_id`
query to actually read the full row -- these two functions exist solely to
answer "which org," nothing else, so the rest of the read path stays under
ordinary RLS enforcement rather than also running with elevated privilege.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2e5f8a3c1b6'
down_revision: str | None = 'c7d4e8f19a2b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION resolve_connector_config_organization(config_id uuid)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT organization_id FROM connector_configs WHERE id = config_id;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION resolve_document_organization(doc_id uuid)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT organization_id FROM documents WHERE id = doc_id;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION list_active_connector_config_ids()
        RETURNS TABLE(id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id FROM connector_configs WHERE status IN ('active', 'error');
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION resolve_refresh_token_organization(token_hash_arg text)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT organization_id FROM refresh_tokens WHERE token_hash = token_hash_arg;
        $$;
        """
    )


def downgrade() -> None:
    op.execute('DROP FUNCTION IF EXISTS resolve_refresh_token_organization(text)')
    op.execute('DROP FUNCTION IF EXISTS list_active_connector_config_ids()')
    op.execute('DROP FUNCTION IF EXISTS resolve_document_organization(uuid)')
    op.execute('DROP FUNCTION IF EXISTS resolve_connector_config_organization(uuid)')
