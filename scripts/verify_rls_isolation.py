"""Live proof that Row-Level Security actually enforces tenant isolation
under the connection role the application is currently configured with.

WHY THIS EXISTS
    `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #2 found that
    every environment observed so far connects as a role with
    `rolbypassrls = true` (Neon's `neondb_owner`, Railway's default Postgres
    user), which makes every RLS policy from migration `c7d4e8f19a2b` a
    silent no-op -- `FORCE ROW LEVEL SECURITY` does not change this; Postgres
    checks `BYPASSRLS` before ever consulting a table's policies. Migration
    `b8f3d6a1c4e7` provisions a correctly-scoped `ekip_app` role
    (`NOSUPERUSER NOBYPASSRLS`), but provisioning the role is not the same
    as proving the application is actually protected once it connects as
    that role.

    This script is that proof, and is the dedicated CI security gate's only
    job (`.github/workflows/rls-security.yml`) -- deliberately independent
    of the OpenAI-dependent `browser-e2e`/`ai-evaluation` tiers in
    `e2e-and-eval.yml`, so a missing `OPENAI_API_KEY` secret can never cause
    tenant-isolation verification to silently not run.

    It does not trust `pg_roles.rolbypassrls` alone (a role could be
    renamed, or `DATABASE_URL` could point somewhere unexpected) -- it
    creates real data for two organizations and then queries it back with a
    deliberately UNSCOPED query (no `WHERE organization_id = ...` at all),
    the exact shape of bug this defense exists to catch if an
    application-level tenant filter is ever accidentally omitted somewhere.
    If RLS is doing its job, that unscoped query still only ever returns the
    one organization's own rows -- the database itself refuses to return the
    other organization's data, regardless of what SQL was sent.

WHY IT IS A SCRIPT, NOT A pytest TEST
    Same reasoning as `scripts/eval_confidence.py` and `scripts/e2e_seed.py`:
    this needs a live Postgres with RLS actually active, which is
    deliberately out of reach of the offline unit-test suite (see
    `tests/database/test_migration_coverage.py`'s own docstring for why that
    suite uses static source scans instead of a live database). This script
    is the live counterpart, run in two places: the dedicated
    `rls-security.yml` CI job (every PR/push, no OpenAI dependency) and
    `browser-e2e`'s existing real-database tier (as a second, redundant
    confirmation alongside the full application test suite).

SAFE TO RE-RUN
    Organizations are looked up by slug and reused if they already exist
    (same pattern as `scripts/e2e_seed.py`); incidents are looked up by
    title and only inserted if missing. No row is ever deleted or mutated.

KNOWN, OUT-OF-SCOPE FINDING THIS SCRIPT WORKS AROUND, NOT SILENTLY FIXES
    `app.core.tenancy.service.create_organization` creates an organization's
    mandatory default `Project` row in the same call, but never calls
    `set_tenant_context` before that insert -- there is no valid
    organization_id to scope it to until the `organizations` row itself has
    committed, and no prior environment has ever run this function against
    a real RLS-enforcing connection to notice. Running this script directly
    against `ekip_app` (2026-09-05) is what first surfaced it: the `projects`
    insert fails outright with `InsufficientPrivilegeError: new row violates
    row-level security policy for table "projects"` -- meaning organization
    signup itself would fail the moment any environment's `DATABASE_URL` is
    actually pointed at `ekip_app`, independent of anything this script
    checks. This is a real, separate, likely-Critical bug -- not something
    this change is authorized to fix (see the task this script was written
    under: "do not modify unrelated application behavior") -- so this
    script bootstraps its own two test organizations one level down, via
    `tenancy_repository.insert_organization` + `set_tenant_context` +
    `tenancy_repository.insert_project` directly, instead of through the
    currently-broken `tenancy_service.create_organization`. Report this
    finding to whoever owns signup before relying on real RLS enforcement
    anywhere signup can run.

EXIT STATUS
    0  -- every check passed: the connected role is `ekip_app`, does not
          bypass RLS and is not a superuser, has exactly the grants it
          needs, every RLS-policed table has RLS enabled and forced, and
          cross-tenant reads are blocked even without an application-level
          filter, while same-tenant reads and inserts work normally.
    1  -- any check failed. The failure message names exactly which
          assertion failed and why it matters.

RUN
    python scripts/verify_rls_isolation.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.incidents import repository as incidents_repository
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy.schemas import Organization
from app.core.users import service as users_service
from app.database.session import session_scope, set_tenant_context
from app.shared.config.logging import configure_logging

configure_logging()

EXPECTED_RUNTIME_ROLE = "ekip_app"
# Representative tenant-owned table used for the grants check below --
# `incidents` is the one this script writes to and reads back anyway, so a
# missing grant here fails at exactly the step that would need it, with a
# clear check name instead of a bare SQLAlchemy traceback.
_GRANTS_CHECK_TABLE = "incidents"
_REQUIRED_GRANTS = {"SELECT", "INSERT", "UPDATE", "DELETE"}

ORG_A_NAME = "RLS Verify Org A"
ORG_A_SLUG = "rls-verify-org-a"
ORG_B_NAME = "RLS Verify Org B"
ORG_B_SLUG = "rls-verify-org-b"

INCIDENT_A_TITLE = "RLS-PROOF-ORG-A-INCIDENT"
INCIDENT_B_TITLE = "RLS-PROOF-ORG-B-INCIDENT"

_FAILURES: list[str] = []


def _check(condition: bool, description: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {description}")
    if not condition:
        _FAILURES.append(description)


async def _get_or_create_organization(session, *, name: str, slug: str) -> Organization:
    """Create `slug`'s organization (with its mandatory default project) if
    it doesn't already exist.

    Deliberately does NOT call `tenancy_service.create_organization` -- see
    this module's own docstring, "KNOWN, OUT-OF-SCOPE FINDING" section, for
    why that composite function currently fails under real RLS. This
    bootstraps the same end state (an organization with one default
    project) one level down, establishing tenant context between the two
    inserts instead of across them.
    """
    existing = await tenancy_repository.get_organization_by_slug(session, slug)
    if existing is not None:
        return Organization.model_validate(existing)

    organization = await tenancy_repository.insert_organization(session, name=name, slug=slug)
    await set_tenant_context(session, organization.id)
    await tenancy_repository.insert_project(
        session, organization_id=organization.id, name="General", is_default=True
    )
    return Organization.model_validate(organization)


async def _ensure_incident(session, *, organization: Organization, title: str) -> uuid.UUID:
    """Create `title`'s incident in `organization` if it doesn't already
    exist, under that organization's own tenant context (this is the normal,
    correctly-scoped write path -- the point of this script is what happens
    on *read*, not that writes themselves are unprotected).
    """
    await set_tenant_context(session, organization.id)

    existing = await session.execute(
        text("SELECT id FROM incidents WHERE organization_id = :org_id AND title = :title"),
        {"org_id": str(organization.id), "title": title},
    )
    row = existing.first()
    if row is not None:
        return row[0]

    user_id = await users_service.get_or_create_user(
        session,
        email=f"rls-verify+{organization.slug}@example.com",
        display_name=f"RLS Verify ({organization.slug})",
    )
    default_project = await tenancy_repository.get_default_project(session, organization.id)
    assert default_project is not None, (
        f"{organization.slug} has no default project -- "
        "_get_or_create_organization is supposed to always create one "
        "alongside the organization itself."
    )
    incident = await incidents_repository.insert_incident(
        session,
        organization_id=organization.id,
        project_id=default_project.id,
        title=title,
        description="Seeded by scripts/verify_rls_isolation.py to prove RLS enforcement.",
        severity="low",
        reported_by=user_id,
    )
    return incident.id


async def _unscoped_incident_titles(session) -> set[str]:
    """The exact query shape this script exists to stress-test: no
    `WHERE organization_id = ...` at all -- simulating an application-level
    tenant filter that was accidentally omitted. If RLS is enforced, the
    database itself still only returns rows belonging to whatever
    organization the current session's tenant context (or connection role,
    absent one) is allowed to see.
    """
    result = await session.execute(
        text(
            "SELECT title FROM incidents "
            "WHERE title IN (:title_a, :title_b)"
        ),
        {"title_a": INCIDENT_A_TITLE, "title_b": INCIDENT_B_TITLE},
    )
    return {row[0] for row in result.all()}


async def main() -> int:
    print("=== EKIP RLS tenant-isolation verification ===\n")

    # --- Step 0: prove the connected role is the one meant to run in
    # production, and cannot bypass RLS in the first place. Every check
    # after this one is meaningless if this fails -- BYPASSRLS makes every
    # RLS policy a silent no-op regardless of how the rest of this script's
    # queries are shaped.
    async with session_scope() as session:
        role_row = await session.execute(
            text(
                "SELECT current_user, "
                "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypassrls, "
                "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser"
            )
        )
        current_role, bypassrls, is_superuser = role_row.one()

    print(f"Connected as role: {current_role!r}")
    _check(
        current_role == EXPECTED_RUNTIME_ROLE,
        f"runtime role is {EXPECTED_RUNTIME_ROLE!r} (was {current_role!r} -- "
        "if this is an admin/migration credential instead, DATABASE_URL is "
        "misconfigured for this check)",
    )
    _check(
        bypassrls is False,
        f"connected role {current_role!r} has rolbypassrls = false "
        "(a role with BYPASSRLS makes every RLS policy in this schema a "
        "silent no-op -- see EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md "
        "recommendation #2)",
    )
    _check(
        is_superuser is False,
        f"connected role {current_role!r} is not a superuser "
        "(superuser also bypasses RLS, independent of the BYPASSRLS attribute)",
    )

    if _FAILURES:
        print(
            "\nStopping early -- the connected role bypasses RLS, so every "
            "remaining check would trivially 'pass' without proving "
            "anything. Point DATABASE_URL at the ekip_app role (migration "
            "b8f3d6a1c4e7) before re-running this script."
        )
        return 1

    # --- Step 0b: the connected role actually holds the DML grants it needs
    # (migration b8f3d6a1c4e7's ALTER DEFAULT PRIVILEGES could in principle
    # not have covered a specific table, e.g. one created by a migration
    # that ran before b8f3d6a1c4e7 without a corresponding backfill).
    async with session_scope() as session:
        grants_row = await session.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND grantee = current_user"
            ),
            {"table": _GRANTS_CHECK_TABLE},
        )
        actual_grants = {row[0] for row in grants_row.all()}
    missing_grants = _REQUIRED_GRANTS - actual_grants
    _check(
        not missing_grants,
        f"{EXPECTED_RUNTIME_ROLE} holds {sorted(_REQUIRED_GRANTS)} on "
        f"{_GRANTS_CHECK_TABLE!r}"
        + (f" (missing: {sorted(missing_grants)})" if missing_grants else ""),
    )

    # --- Step 0c: every table with at least one RLS policy defined must
    # have RLS both enabled AND forced -- `ENABLE` alone still exempts the
    # table's owner (see migration c7d4e8f19a2b's own docstring on why
    # FORCE is required), so this checks both flags, not just one.
    async with session_scope() as session:
        rls_row = await session.execute(
            text(
                "SELECT DISTINCT p.tablename, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_policies p "
                "JOIN pg_class c ON c.relname = p.tablename "
                "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = p.schemaname "
                "WHERE p.schemaname = 'public'"
            )
        )
        rls_tables = rls_row.all()
    _check(
        len(rls_tables) > 0,
        "at least one table in schema public has an RLS policy defined "
        "(a suspiciously empty result here means migration c7d4e8f19a2b "
        "never actually ran, not that there's nothing to protect)",
    )
    not_enabled = [t for t, enabled, _forced in rls_tables if not enabled]
    not_forced = [t for t, _enabled, forced in rls_tables if not forced]
    _check(
        not not_enabled,
        "every policy-bearing table has RLS enabled (relrowsecurity)"
        + (f" (not enabled: {not_enabled})" if not_enabled else ""),
    )
    _check(
        not not_forced,
        "every policy-bearing table has RLS forced (relforcerowsecurity) "
        "-- required for the table owner to be protected too, not just "
        "other roles"
        + (f" (not forced: {not_forced})" if not_forced else ""),
    )

    # --- Step 1: seed two organizations, each with one incident, under
    # each organization's own correctly-scoped tenant context.
    async with session_scope() as session:
        org_a = await _get_or_create_organization(session, name=ORG_A_NAME, slug=ORG_A_SLUG)
        org_b = await _get_or_create_organization(session, name=ORG_B_NAME, slug=ORG_B_SLUG)
        await _ensure_incident(session, organization=org_a, title=INCIDENT_A_TITLE)
        await _ensure_incident(session, organization=org_b, title=INCIDENT_B_TITLE)

    print(f"\nOrg A: {org_a.id} ({org_a.slug})")
    print(f"Org B: {org_b.id} ({org_b.slug})")

    # --- Step 2: same-tenant reads still work normally (RLS must not be so
    # strict it breaks legitimate access).
    async with session_scope() as session:
        await set_tenant_context(session, org_a.id)
        titles_as_a = await _unscoped_incident_titles(session)
    _check(
        INCIDENT_A_TITLE in titles_as_a,
        "Org A's tenant context can still read Org A's own incident "
        "(same-tenant access is not broken by RLS)",
    )

    # --- Step 3: cross-tenant reads are blocked even with NO
    # application-level filter in the query at all -- the core proof.
    _check(
        INCIDENT_B_TITLE not in titles_as_a,
        "Org A's tenant context, running a query with NO organization_id "
        "filter, does NOT see Org B's incident (this is what RLS is for: "
        "protection even when an application-level tenant filter is "
        "accidentally omitted)",
    )

    async with session_scope() as session:
        await set_tenant_context(session, org_b.id)
        titles_as_b = await _unscoped_incident_titles(session)
    _check(
        INCIDENT_B_TITLE in titles_as_b,
        "Org B's tenant context can still read Org B's own incident",
    )
    _check(
        INCIDENT_A_TITLE not in titles_as_b,
        "Org B's tenant context, running the same unscoped query, does NOT "
        "see Org A's incident",
    )

    # --- Step 4: fail-closed with no tenant context set at all (the
    # pre-tenant-context state every request starts in before
    # set_tenant_context is called).
    #
    # Two safe outcomes are possible here, and this script treats both as a
    # PASS -- what matters is that neither organization's row is ever
    # returned, not which of the two shapes "no data" takes:
    #
    #   1. Zero rows. `current_setting('app.current_organization_id', true)`
    #      returns NULL, `NULL::uuid = organization_id` is NULL (never TRUE)
    #      for every row -- the documented behavior in
    #      `app.database.session.set_tenant_context`'s own docstring.
    #
    #   2. A raised `invalid input syntax for type uuid: ""` error. Verified
    #      directly against this connection pool (2026-09-05): once ANY
    #      transaction on a given pooled physical connection has called
    #      `set_config(..., true)` even once, Postgres's reset value for
    #      that now-known custom GUC becomes `''` for the rest of that
    #      backend's life, not NULL -- `''::uuid` raises rather than
    #      compares false. This is a real Postgres semantic for
    #      previously-touched custom GUCs (confirmed with a bare `psql`
    #      session, independent of this application's connection pooling),
    #      not an application bug, and the outcome is still fail-closed --
    #      an aborted query returns no rows to anyone either. It does mean
    #      `set_tenant_context`'s docstring ("a session that never calls
    #      this sees zero rows") is only literally accurate for a brand-new
    #      physical connection, not a reused pooled one; worth a docstring
    #      correction, not a security fix.
    try:
        async with session_scope() as session:
            titles_no_context = await _unscoped_incident_titles(session)
    except DBAPIError as exc:
        if "invalid input syntax for type uuid" in str(exc.orig):
            titles_no_context = set()
        else:
            raise
    _check(
        len(titles_no_context) == 0,
        "a session with NO tenant context set sees ZERO incidents from "
        "either organization -- either an empty result or Postgres "
        "refusing the query outright, never actual cross-tenant data "
        "(fail-closed)",
    )

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED:")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1

    print("All checks passed: RLS is enforced end-to-end under this role.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
