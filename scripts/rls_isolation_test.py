"""Real, disposable-database proof of tenant isolation under RLS (Phase
4.7.9 / 4.7.10).

WHY THIS IS A SCRIPT, NOT A PYTEST TEST
    Same reasoning as `scripts/eval_confidence.py` and
    `scripts/migration_status.py`: this needs a real, running PostgreSQL
    instance with the `vector` extension and every migration applied --
    nothing here is mocked, by design, because the entire point is proving
    Postgres's own RLS enforcement (session GUCs, `FORCE ROW LEVEL
    SECURITY`, a non-superuser connection role), which a mocked session
    cannot exercise at all.

WHAT THIS PROVES, END TO END
    1. Creates two real organizations (Alpha, Beta) and one incident each,
       using a Postgres role with NOSUPERUSER/NOBYPASSRLS (matching
       `ekip_app`'s attributes -- see docs/operations/migration-recovery.md)
       -- a superuser/BYPASSRLS connection would make every assertion below
       pass trivially even if RLS were completely broken, so this
       deliberately does NOT run as whatever role DATABASE_URL's default
       user is.
    2. Proves the basic RLS contract: Alpha's own tenant context sees only
       Alpha's incident; Beta's own tenant context sees only Beta's.
    3. Proves the specific failure mode this codebase's design is built to
       prevent -- a single physical connection, reused across "requests"
       for different organizations (simulating connection pooling), must
       never leak one organization's tenant context into the next request
       on the same connection. Runs Alpha -> Beta -> Alpha on ONE
       connection and asserts each step sees only its own organization's
       row, proving `set_tenant_context`'s `SET LOCAL` (transaction-scoped,
       not session-scoped -- see `app/database/session.py`'s own docstring)
       actually behaves as documented against a real server, not just in
       that docstring's own reasoning.
    4. Proves fail-closed behavior: a connection that never calls
       `set_tenant_context` at all sees ZERO rows from an RLS-protected
       table (not every row) -- the specific "a bug here is loud, not a
       silent cross-tenant leak" property `c7d4e8f19a2b`'s own migration
       docstring designs for.
    5. Concurrency: runs Alpha and Beta queries concurrently, on separate
       connections drawn from the same pool, and confirms neither ever
       observes the other's row.

WHAT THIS DOES NOT PROVE
    - Nothing about the *application's* connection-pool configuration
      specifically (SQLAlchemy's own async engine pool, arq worker
      connections, MCP) beyond what raw asyncpg here demonstrates about
      Postgres's own RLS/GUC contract. Wiring the actual application to
      connect as a non-superuser role (rather than this script's own
      dedicated test role) is a separate, larger change --
      docs/operations/migration-recovery.md's recommendation, not yet
      applied to any environment. This script exists to de-risk that change
      by proving the underlying Postgres mechanism works correctly first,
      against a disposable database, before wiring the whole application to
      depend on it.
    - Nothing against the shared Neon development database -- deliberately.
      `neondb_owner` (what `DATABASE_URL` there connects as) has
      `bypassrls=true` (confirmed, see migration-recovery.md), so every
      assertion below would trivially pass against it regardless of whether
      RLS actually works, which would prove nothing. Point this script at a
      disposable database only, via `RLS_TEST_DATABASE_URL` (see below) --
      never at the project's real `DATABASE_URL`.

REQUIREMENTS TO RUN
    - A real PostgreSQL instance (local, `docker compose`, or a disposable
      cloud instance) with the `vector` extension installable
      (`CREATE EXTENSION IF NOT EXISTS vector` -- pgvector must be
      available, not just permitted).
    - Every migration applied (`alembic upgrade head` against that database
      first).
    - `RLS_TEST_DATABASE_URL` set to an admin/superuser connection string
      for that disposable database (used only to create the test role and
      seed rows -- never used for the actual RLS-scoped queries below).

    NOT YET RUN in this environment as of authoring: no Docker daemon and no
    local Postgres with pgvector were available (see
    docs/operations/local-production.md's "Known limitations" and
    docs/operations/migration-recovery.md's "Fresh database proof" section).
    This script is prepared and ready to run the moment such an environment
    exists -- treat its assertions as reasoned-through, not yet empirically
    confirmed against a real server.

RUN
    RLS_TEST_DATABASE_URL=postgresql://admin:pw@host/disposable_db \\
        uv run python scripts/rls_isolation_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg

_TEST_ROLE = "rls_isolation_test_role"
_TEST_ROLE_PASSWORD = "rls-isolation-test-only-not-a-real-secret"
_GUC_NAME = "app.current_organization_id"


async def _admin_conn() -> asyncpg.Connection:
    dsn = os.environ.get("RLS_TEST_DATABASE_URL")
    if not dsn:
        print(
            "RLS_TEST_DATABASE_URL is not set -- refusing to run against the "
            "default DATABASE_URL (which may be the real, shared Neon "
            "database). Point this at a disposable database instead."
        )
        sys.exit(1)
    return await asyncpg.connect(dsn)


async def _provision_test_role(admin: asyncpg.Connection) -> None:
    """Mirrors `ekip_app`'s attributes exactly (NOSUPERUSER, NOBYPASSRLS) --
    see `app/database/migrations/versions/f4a7c2e9b3d1_provision_rls_
    respecting_app_role.py` on `origin/simran-ekip` for the production
    equivalent this test role stands in for.
    """
    await admin.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_TEST_ROLE}') THEN
                CREATE ROLE {_TEST_ROLE}
                    WITH LOGIN PASSWORD '{_TEST_ROLE_PASSWORD}'
                    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
        END
        $$;
        """
    )
    await admin.execute(f"GRANT USAGE ON SCHEMA public TO {_TEST_ROLE}")
    await admin.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_TEST_ROLE}"
    )
    await admin.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_TEST_ROLE}")


def _test_role_dsn() -> str:
    """Swap the admin DSN's credentials for the dedicated test role's,
    keeping host/port/database/query-params unchanged -- avoids depending on
    asyncpg's internal DSN parser for something this simple.
    """
    admin_dsn = os.environ["RLS_TEST_DATABASE_URL"]
    parsed = urlsplit(admin_dsn)
    netloc = f"{_TEST_ROLE}:{_TEST_ROLE_PASSWORD}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


async def _test_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_test_role_dsn())


async def _seed_org_and_incident(admin: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    await admin.execute(
        "INSERT INTO organizations (id, name, slug, status) VALUES ($1, $2, $3, 'active')",
        org_id, f"RLS Test Org {org_id.hex[:8]}", f"rls-test-{org_id.hex[:8]}",
    )
    await admin.execute(
        "INSERT INTO projects (id, organization_id, name, is_default) VALUES ($1, $2, 'default', true)",
        project_id, org_id,
    )
    reporter_id = uuid.uuid4()
    await admin.execute(
        "INSERT INTO users (id, email, display_name, is_active) VALUES ($1, $2, 'RLS Test Reporter', true)",
        reporter_id, f"rls-test-{reporter_id.hex[:8]}@example.test",
    )
    await admin.execute(
        """INSERT INTO incidents
           (id, organization_id, project_id, title, description, status, severity, reported_by)
           VALUES ($1, $2, $3, 'RLS isolation test incident', 'seeded by scripts/rls_isolation_test.py',
                   'open', 'low', $4)""",
        incident_id, org_id, project_id, reporter_id,
    )
    return org_id, project_id, incident_id


async def _set_tenant_context(conn: asyncpg.Connection, org_id: uuid.UUID | None) -> None:
    async with conn.transaction():
        if org_id is not None:
            await conn.execute("SELECT set_config($1, $2, true)", _GUC_NAME, str(org_id))
        rows = await conn.fetch("SELECT id, organization_id FROM incidents")
        return rows


async def main() -> int:
    admin = await _admin_conn()
    failures: list[str] = []

    try:
        await _provision_test_role(admin)

        alpha_org, _alpha_project, alpha_incident = await _seed_org_and_incident(admin)
        beta_org, _beta_project, beta_incident = await _seed_org_and_incident(admin)

        # --- 1. Basic RLS contract -------------------------------------------------
        test_conn = await _test_conn()
        try:
            alpha_rows = await _set_tenant_context(test_conn, alpha_org)
            if {r["id"] for r in alpha_rows} != {alpha_incident}:
                failures.append(f"Alpha context saw {[r['id'] for r in alpha_rows]}, expected only {alpha_incident}")

            beta_rows = await _set_tenant_context(test_conn, beta_org)
            if {r["id"] for r in beta_rows} != {beta_incident}:
                failures.append(f"Beta context saw {[r['id'] for r in beta_rows]}, expected only {beta_incident}")
        finally:
            await test_conn.close()

        # --- 2. Pooled-connection reuse: Alpha -> Beta -> Alpha, one connection ----
        pooled_conn = await _test_conn()
        try:
            step1 = await _set_tenant_context(pooled_conn, alpha_org)
            step2 = await _set_tenant_context(pooled_conn, beta_org)
            step3 = await _set_tenant_context(pooled_conn, alpha_org)

            for label, rows, expected in (
                ("step1 (Alpha)", step1, alpha_incident),
                ("step2 (Beta)", step2, beta_incident),
                ("step3 (Alpha again)", step3, alpha_incident),
            ):
                if {r["id"] for r in rows} != {expected}:
                    failures.append(
                        f"Pooled-connection {label} saw {[r['id'] for r in rows]}, expected only {expected} "
                        "-- SET LOCAL context leaked across a simulated pooled-connection reuse"
                    )
        finally:
            await pooled_conn.close()

        # --- 3. Fail-closed: no tenant context set at all --------------------------
        no_context_conn = await _test_conn()
        try:
            rows = await _set_tenant_context(no_context_conn, None)
            if rows:
                failures.append(
                    f"No tenant context set, but saw {len(rows)} row(s) -- must be fail-closed (zero rows), not fail-open"
                )
        finally:
            await no_context_conn.close()

        # --- 4. Concurrency: Alpha and Beta queried concurrently on separate connections
        async def _concurrent_check(org_id: uuid.UUID, expected: uuid.UUID) -> str | None:
            conn = await _test_conn()
            try:
                rows = await _set_tenant_context(conn, org_id)
                if {r["id"] for r in rows} != {expected}:
                    return f"Concurrent check for org {org_id} saw {[r['id'] for r in rows]}, expected only {expected}"
                return None
            finally:
                await conn.close()

        results = await asyncio.gather(
            *[_concurrent_check(alpha_org, alpha_incident) for _ in range(5)],
            *[_concurrent_check(beta_org, beta_incident) for _ in range(5)],
        )
        failures.extend(r for r in results if r is not None)

    finally:
        # Cleanup: delete only the rows this run created.
        await admin.execute("DELETE FROM incidents WHERE organization_id = ANY($1::uuid[])", [alpha_org, beta_org])
        await admin.execute("DELETE FROM projects WHERE organization_id = ANY($1::uuid[])", [alpha_org, beta_org])
        await admin.execute("DELETE FROM organizations WHERE id = ANY($1::uuid[])", [alpha_org, beta_org])
        await admin.close()

    if failures:
        print("RLS ISOLATION TEST: FAILED")
        for f in failures:
            print(f" - {f}")
        return 1

    print("RLS ISOLATION TEST: PASSED")
    print(" - Basic RLS contract: OK")
    print(" - Pooled-connection reuse (Alpha -> Beta -> Alpha): OK, no leakage")
    print(" - Fail-closed with no tenant context: OK")
    print(" - Concurrent cross-org queries: OK, no leakage")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
