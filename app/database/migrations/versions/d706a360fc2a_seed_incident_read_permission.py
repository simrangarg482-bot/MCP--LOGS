"""seed incident:read permission and backfill every existing role

Revision ID: d706a360fc2a
Revises: 90ff736ced55
Create Date: 2026-08-18 11:56:15.360854

Phase 4.7.2 fix: closes a confirmed access-control gap (2026-08 audit "H4",
independently re-confirmed during Batch 4.6/4.7 -- see
`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #3) where
`app.core.incidents.service.get_incident` / `list_incidents` / `get_timeline`
checked only same-organization membership, with no permission check at all --
unlike every write path in the same module, which already requires
`incident:write`. Any identity in the organization, including one with zero
role assignments, could read every incident's full detail and timeline
across every project.

This is the migration half of that fix; the code half
(`require_project_permission`/`require_permission(actor, ...,
"incident:read")` in the three functions above) already ships in this same
change. This migration must land in the same release as that code change --
shipping the code without this migration would lock every existing identity
out of incident reads entirely (no role would hold the new permission yet).

Only `incident:read` is added here, not `postmortem:read` -- unlike the
abandoned `origin/simran-ekip` branch's equivalent migration
(`b6e9c2a4f7d1`, see `docs/operations/migration-recovery.md`), `main`'s
`get_postmortem`/`get_postmortem_by_incident` already gate draft/in-review
postmortems on `postmortem:write`/`postmortem:approve` and leave
approved/published open to any org member -- a different, already-correct
design that has no gap `postmortem:read` would close. Reusing existing
permission codes here rather than introducing an unused one, per this
phase's "do not invent new permissions unless absolutely necessary" rule.

Idempotency: `ON CONFLICT DO NOTHING` throughout -- safe to re-run against a
database that already has this permission/grants (matching the idempotency
discipline every other permission-seeding migration in this history uses).

Backfill choice: granted to **every existing role**, not a curated subset --
the only backward-compatible choice, since preserving already-provisioned
identities' pre-existing (if accidental) read access is this migration's
entire purpose. A company wanting a genuinely read-restricted role from this
point forward creates one without this grant going forward; this migration
only protects already-provisioned data, it does not weaken the new gate's
ability to restrict future roles.

`permissions`/`roles`/`role_permissions` are excluded from Row-Level
Security (`c7d4e8f19a2b_milestone_10_row_level_security.py`'s own
"genuinely global catalogs" list) -- no `set_tenant_context`/GUC call is
needed here.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd706a360fc2a'
down_revision: str | None = '90ff736ced55'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "incident:read"
_PERMISSION_DESCRIPTION = "Read an incident, its timeline, and its postmortem (2026-08 audit 'H4')."


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            "INSERT INTO permissions (code, description) "
            "VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"code": _PERMISSION_CODE, "description": _PERMISSION_DESCRIPTION},
    )

    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            "CROSS JOIN permissions p "
            "WHERE p.code = :code "
            "ON CONFLICT DO NOTHING"
        ),
        {"code": _PERMISSION_CODE},
    )


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN ("
            "SELECT id FROM permissions WHERE code = :code"
            ")"
        ),
        {"code": _PERMISSION_CODE},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code = :code"),
        {"code": _PERMISSION_CODE},
    )
