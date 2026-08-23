"""provision ekip_app, a dedicated non-superuser/non-bypassrls application role

Revision ID: b8f3d6a1c4e7
Revises: 1269a7b553a9
Create Date: 2026-08-18 00:00:00.000000

CONFIRMED, still-open finding (`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md`
recommendation #2, re-confirmed via direct `pg_roles` introspection during
the Batch 4.6 migration investigation): every environment this application
has ever run in connects as `neondb_owner`, which has `rolbypassrls=true`.
Migration `c7d4e8f19a2b`'s `FORCE ROW LEVEL SECURITY` makes every RLS policy
apply even to a table's owner, but neither `FORCE` nor the policies
themselves have any effect at all on a role carrying `BYPASSRLS` -- Postgres
checks that attribute before ever consulting a table's RLS configuration.
Every RLS policy in this schema is therefore a live no-op against real
traffic today, regardless of how carefully migration `c7d4e8f19a2b`/
`d2e5f8a3c1b6` were designed and reviewed.

PROVENANCE -- this is not new work invented from scratch. A role named
`ekip_app`, with exactly this shape (`NOSUPERUSER NOBYPASSRLS NOCREATEDB
NOCREATEROLE`), already exists in the project's shared Neon database today --
created by migration `f4a7c2e9b3d1` on the unmerged `origin/simran-ekip`
branch, `down_revision='e3f6a1b8d4c9'` (an old point in that branch's own
history, well before this repository's current head). `main` never adopted
that migration; migration `90ff736ced55` deliberately did NOT drop the role
it left behind, precisely because it represented real, correct progress on
this same open recommendation (see that migration's own docstring). This
migration reuses `f4a7c2e9b3d1`'s design and grant list near-verbatim
(re-read from that branch's history for this purpose, not merged), rechained
onto `main`'s own current head instead of its original, long-stale parent --
`main`'s schema has moved on considerably since `e3f6a1b8d4c9`, but nothing
about *this* migration's logic depends on which tables exist by name: every
statement below operates on "every table/sequence/function in schema
public," present or future, not a fixed list. Running this migration a
second time (e.g. against Neon, where `ekip_app` already exists in this
exact shape) is intentionally idempotent -- it converges the role to this
definition rather than failing on a duplicate.

PRIVILEGES GRANTED: ordinary DML (`SELECT`/`INSERT`/`UPDATE`/`DELETE`) on
every table in `public`, matching grants on sequences, and -- via `ALTER
DEFAULT PRIVILEGES`, covering tables/sequences/functions created by *later*
migrations automatically -- the same for anything not yet created when this
migration runs. This role is never granted `BYPASSRLS`, `SUPERUSER`,
`CREATEDB`, or `CREATEROLE`: RLS applies to every one of its queries exactly
as designed, which is the entire point.

SECURITY DEFINER functions (`d2e5f8a3c1b6`'s four narrow RLS-bypass
functions, plus `c5e2a9f4d7b3` immediately after this one) are unaffected by
which role calls them: `SECURITY DEFINER` always runs with the privileges of
the function's *owner* (the migration-running role), never the caller's.
Postgres grants `EXECUTE` on a newly created function to `PUBLIC` by default,
and no migration in this repository revokes that (confirmed: no `REVOKE
EXECUTE` appears anywhere in `app/database/migrations/versions/`) -- so
`ekip_app` can already call every one of them without any additional grant.

WHAT THIS MIGRATION DOES NOT DO: it does not change which role `DATABASE_URL`
actually connects as anywhere. Provisioning this role is necessary but not
sufficient -- an operator must still update the deployed `DATABASE_URL`
secret (in Neon's own dashboard, `.env`, `infra/main.bicep`'s
`sharedSecrets`, etc.) to connect as `ekip_app` with the same password
supplied to this migration, instead of whichever role currently owns the
schema. That is a deployment-secret/live-database change outside what any
migration can perform by itself, and stays a deliberate, explicit, separately
-authorized manual step -- see EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md's
recommendation #2 and docs/operations/migration-recovery.md for the full
rollout checklist. Running this migration against Neon at all (even though
it converges to a state Neon already has) still requires the same explicit
authorization every other live-Neon action in this project's history has
required -- it is not applied as a side effect of writing this file.

PASSWORD HANDLING: role-DDL statements (`CREATE ROLE ... PASSWORD`) cannot
take a bind parameter -- the same constraint `app.database.session.
set_tenant_context`'s docstring documents for `SET LOCAL`. Hardcoding a real
password into a checked-in migration file would itself be exactly the kind
of secret-handling regression this project's own incident history
(`docs/operations/security-incidents.md`) exists to prevent. This migration
therefore reads `EKIP_APP_ROLE_PASSWORD` from the environment *at
migration-run time* and fails loudly if it is unset, rather than silently
falling back to a guessable default.
"""
from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8f3d6a1c4e7'
down_revision: str | None = '1269a7b553a9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "ekip_app"
_PASSWORD_ENV_VAR = "EKIP_APP_ROLE_PASSWORD"


def _escape_sql_literal(value: str) -> str:
    """Escape a value for interpolation into a single-quoted SQL string
    literal. Role-DDL statements can't use bind parameters (see module
    docstring), so this is the one place a value must be interpolated
    directly -- doubling embedded single quotes is the standard SQL
    escaping rule for that case.
    """
    return value.replace("'", "''")


def _current_database(bind) -> str:
    return bind.execute(sa.text("SELECT current_database()")).scalar_one()


def upgrade() -> None:
    password = os.environ.get(_PASSWORD_ENV_VAR)
    if not password:
        raise RuntimeError(
            f"{_PASSWORD_ENV_VAR} must be set in the environment running this "
            f"migration (e.g. `{_PASSWORD_ENV_VAR}=... alembic upgrade head`) -- "
            f"refusing to create the '{_APP_ROLE}' role with a hardcoded or "
            "empty password."
        )
    escaped_password = _escape_sql_literal(password)

    bind = op.get_bind()
    db_name = _current_database(bind)

    # CREATE ROLE has no `IF NOT EXISTS` clause -- re-running this migration
    # against a database that already has the role (e.g. Neon, where it
    # already exists from the abandoned branch) must converge its
    # password/attributes to this definition, not fail.
    # `NOSUPERUSER` is deliberately absent from both branches below, not an
    # oversight: Postgres enforces "only roles with the SUPERUSER attribute
    # may change the SUPERUSER attribute" as an absolute rule, independent
    # of CREATEROLE -- confirmed live against Neon, whose `neondb_owner`
    # (`rolsuper=false`, but `rolcreatedb`/`rolcreaterole`/`rolreplication`/
    # `rolbypassrls` all `true`) can freely manage every attribute below
    # except this one, and errors outright ("permission denied to alter
    # role") the moment `NOSUPERUSER`/`SUPERUSER` appears in the statement
    # at all -- even to explicitly (redundantly) set a role to the same
    # `NOSUPERUSER` state it already has. Omitting the clause entirely is
    # both sufficient and correct: `NOSUPERUSER` is Postgres's own default
    # for `CREATE ROLE` when unspecified, and `ALTER ROLE` simply leaves an
    # untouched attribute as whatever it already was -- `ekip_app` was
    # never going to be superuser either way, so there is no security
    # difference, only a permissions-model one, between stating this
    # explicitly and relying on the default.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                CREATE ROLE {_APP_ROLE}
                    WITH LOGIN
                    PASSWORD '{escaped_password}'
                    NOBYPASSRLS
                    NOCREATEDB
                    NOCREATEROLE
                    NOREPLICATION;
            ELSE
                ALTER ROLE {_APP_ROLE}
                    WITH LOGIN
                    PASSWORD '{escaped_password}'
                    NOBYPASSRLS
                    NOCREATEDB
                    NOCREATEROLE
                    NOREPLICATION;
            END IF;
        END
        $$;
        """
    )

    op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO {_APP_ROLE}')
    op.execute(f'GRANT USAGE ON SCHEMA public TO {_APP_ROLE}')
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}'
    )
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE}')
    op.execute(f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_APP_ROLE}')

    # Cover tables/sequences/functions added by migrations that run after
    # this one (including this same migration file's own successor,
    # c5e2a9f4d7b3's new SECURITY DEFINER function), without needing a
    # follow-up migration every time.
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}'
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT USAGE, SELECT ON SEQUENCES TO {_APP_ROLE}'
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT EXECUTE ON FUNCTIONS TO {_APP_ROLE}'
    )


def downgrade() -> None:
    bind = op.get_bind()
    db_name = _current_database(bind)

    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_APP_ROLE}'
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'REVOKE USAGE, SELECT ON SEQUENCES FROM {_APP_ROLE}'
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'REVOKE EXECUTE ON FUNCTIONS FROM {_APP_ROLE}'
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {_APP_ROLE}')
    op.execute(f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {_APP_ROLE}')
    op.execute(f'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {_APP_ROLE}')
    op.execute(f'REVOKE USAGE ON SCHEMA public FROM {_APP_ROLE}')
    op.execute(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM {_APP_ROLE}')
    op.execute(f'DROP ROLE IF EXISTS {_APP_ROLE}')
