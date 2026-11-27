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

ORGANIZATION CREATION UNDER RLS (history, and what is checked now)
    An earlier version of this docstring (2026-09-05) reported that
    `tenancy_service.create_organization` failed under a real RLS-enforcing
    role because it inserted the default `projects` row before setting any
    tenant context. That was fixed in `create_organization` itself (it now
    sets the new organization's context before the project insert), so this
    script's own bootstrap (`_get_or_create_organization`, which builds the
    same end state through the repositories so it stays idempotent by slug)
    is no longer a workaround for a broken function.

    A SECOND, related defect was found by reproducing the flows against real
    Postgres as `ekip_app` (2026-11-12): for an authenticated caller,
    `create_organization` switched the connection's tenant context to the NEW
    organization and only then wrote the audit row, which belongs to the
    CALLER's organization -- the `audit_logs` policy rejected it, so
    `POST /organizations` failed and rolled back. The audit write now happens
    first, under the caller's context, and the caller's context is restored
    afterwards. `_organization_creation_checks` keeps this honest against a
    real database: it runs authenticated creation, unauthenticated creation,
    signup (including role assignment) and returning-SSO-user resolution
    inside ONE transaction that is rolled back, then verifies nothing
    remains. It makes no persistent writes.

EXTENDED CHECKS (live-evidence credentials and the incident timeline)
    After the original incident checks, `_extended_checks` proves the same
    cross-tenant guarantees for the two other tables the Investigation Agent
    touches, and for the code paths built on them:

      * `connector_configs` -- an unscoped read as Org A never returns Org B's
        connector (and vice versa), and a session with no tenant context sees
        none of either.
      * `incident_timeline` -- same, for investigation entries.
      * `tenancy_service.resolve_live_evidence_connectors` -- returns exactly
        the requesting organization's decrypted credential (real envelope
        encryption round trip), never the other organization's; returns
        nothing when the session's tenant context is another organization's;
        refuses an incident belonging to another organization.
      * `incidents_service.get_incident` / `record_investigation_result` --
        Org A's actor cannot read or write Org B's incident, an actor
        without `incident:read` cannot write an investigation entry to its
        own organization's incident, and none of the rejected writes leaves a
        timeline row behind.

    The extended section WRITES rows (two throwaway connectors with dummy,
    non-secret tokens, and one timeline entry per organization), so it only
    runs against a loopback database (`127.0.0.1`/`localhost`/`::1` -- the
    disposable CI Postgres or a local container). Against any other host it
    is skipped with a clear message, unless `EKIP_RLS_VERIFY_EXTENDED=1` is
    set explicitly. NEVER point it at a shared or production database.

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
import os
import sys
import uuid
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth import email_verification
from app.core.auth import repository as auth_repository
from app.core.auth import service as auth_service
from app.core.auth.schemas import SignupRequest, VerifiedIdPClaims
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.incidents import repository as incidents_repository
from app.core.incidents import service as incidents_service
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import Organization, OrganizationCreate
from app.core.users import service as users_service
from app.database.session import async_session_factory, session_scope, set_tenant_context
from app.shared.config.logging import configure_logging
from app.shared.config.settings import get_settings
from app.shared.schemas import ActorKind, Identity, InvestigationResult
from app.shared.security import encrypt_secret, get_kms

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

# Extended checks (connector_configs / incident_timeline / live credentials).
# Dummy values: these are not real secrets and are written to the disposable
# database only.
CONNECTOR_MARKER_KEY = "rls_marker"  # keep in sync with the SQL literals below
CONNECTOR_A_MARKER = "RLS-PROOF-CONNECTOR-ORG-A"
CONNECTOR_B_MARKER = "RLS-PROOF-CONNECTOR-ORG-B"
TOKEN_A = "rls-verify-dummy-token-org-a"  # noqa: S105 -- not a real credential
TOKEN_B = "rls-verify-dummy-token-org-b"  # noqa: S105 -- not a real credential
TIMELINE_A_MARKER = "RLS-PROOF-TIMELINE-ORG-A"
TIMELINE_B_MARKER = "RLS-PROOF-TIMELINE-ORG-B"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_FAILURES: list[str] = []


def _check(condition: bool, description: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {description}")
    if not condition:
        _FAILURES.append(description)


async def _get_or_create_organization(session, *, name: str, slug: str) -> Organization:
    """Create `slug`'s organization (with its mandatory default project) if
    it doesn't already exist.

    Builds the end state (an organization with one default project) one
    level down, through the repositories, so seeding stays idempotent by
    slug and does not itself depend on the code under test. The real
    `tenancy_service.create_organization` flow is verified separately, in a
    rolled-back transaction, by `_organization_creation_checks` -- see this
    module's docstring, "ORGANIZATION CREATION UNDER RLS".
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


def _extended_checks_allowed() -> tuple[bool, str]:
    """The extended section writes rows, so it is limited to a loopback
    database unless explicitly forced.
    """
    host = urlsplit(str(get_settings().database_url)).hostname or ""
    if host in _LOOPBACK_HOSTS:
        return True, host
    if os.environ.get("EKIP_RLS_VERIFY_EXTENDED") == "1":
        return True, host
    return False, host


def _investigator(organization_id: uuid.UUID, *, permissions: set[str]) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=f"rls-verify-{uuid.uuid4()}",
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset(permissions),
    )


async def _ensure_connector(
    session, *, organization: Organization, marker: str, token: str
) -> uuid.UUID:
    """One active, org-wide github connector per organization, carrying a
    dummy token encrypted through the REAL envelope-encryption path.
    """
    await set_tenant_context(session, organization.id)
    existing = await session.execute(
        text(
            "SELECT id FROM connector_configs "
            "WHERE organization_id = :org_id AND config->>'rls_marker' = :marker"
        ),
        {"org_id": str(organization.id), "marker": marker},
    )
    row = existing.first()
    if row is not None:
        return row[0]
    created = await tenancy_repository.insert_connector_config(
        session,
        organization_id=organization.id,
        source="github",
        credential_ref=await encrypt_secret(get_kms(), token),
        project_id=None,
        config={CONNECTOR_MARKER_KEY: marker, "repos": []},
        status="active",
    )
    return created.id


async def _ensure_timeline_entry(
    session, *, organization: Organization, incident_id: uuid.UUID, marker: str
) -> None:
    """One investigation timeline entry, written through the REAL
    `record_investigation_result` (so this also exercises its authorization
    for a legitimate read-only investigator).
    """
    await set_tenant_context(session, organization.id)
    existing = await session.execute(
        text(
            "SELECT id FROM incident_timeline "
            "WHERE incident_id = :incident_id "
            "AND event_data->'suggested_next_steps'->>0 = :marker"
        ),
        {"incident_id": str(incident_id), "marker": marker},
    )
    if existing.first() is not None:
        return
    await incidents_service.record_investigation_result(
        session,
        _investigator(organization.id, permissions={"incident:read"}),
        organization.id,
        incident_id,
        InvestigationResult(
            evidence=[],
            hypotheses=[],
            suggested_owner_team=None,
            suggested_next_steps=[marker],
        ),
    )


async def _unscoped_connector_markers(session) -> set[str]:
    result = await session.execute(
        text(
            "SELECT config->>'rls_marker' FROM connector_configs "
            "WHERE config->>'rls_marker' IN (:marker_a, :marker_b)"
        ),
        {"marker_a": CONNECTOR_A_MARKER, "marker_b": CONNECTOR_B_MARKER},
    )
    return {row[0] for row in result.all()}


async def _unscoped_timeline_markers(session) -> set[str]:
    result = await session.execute(
        text(
            "SELECT event_data->'suggested_next_steps'->>0 FROM incident_timeline "
            "WHERE event_data->'suggested_next_steps'->>0 IN (:marker_a, :marker_b)"
        ),
        {"marker_a": TIMELINE_A_MARKER, "marker_b": TIMELINE_B_MARKER},
    )
    return {row[0] for row in result.all()}


async def _without_tenant_context(query) -> set[str]:
    """Run `query(session)` with no tenant context. Either zero rows or
    Postgres refusing the query outright is fail-closed -- see Step 4 in
    `main` for why both shapes are possible.
    """
    try:
        async with session_scope() as session:
            return await query(session)
    except DBAPIError as exc:
        if "invalid input syntax for type uuid" in str(exc.orig):
            return set()
        raise


async def _raises(exception_type: type[Exception], operation) -> bool:
    """True only if `operation()` raises exactly this kind of denial. Any
    other outcome -- success, or a different exception -- is a failed check
    (a different exception is reported, not allowed to crash the run).
    """
    try:
        await operation()
    except exception_type:
        return True
    except Exception as exc:  # noqa: BLE001 -- reported as a failed check below
        print(f"      (unexpected {type(exc).__name__}: {exc})")
        return False
    return False


async def _extended_checks(org_a: Organization, org_b: Organization) -> None:
    print("\n--- Extended checks: connector_configs, incident_timeline, live credentials ---")

    # Seed (each under its own organization's tenant context).
    async with session_scope() as session:
        await _ensure_connector(
            session, organization=org_a, marker=CONNECTOR_A_MARKER, token=TOKEN_A
        )
        await _ensure_connector(
            session, organization=org_b, marker=CONNECTOR_B_MARKER, token=TOKEN_B
        )
    async with session_scope() as session:
        incident_a_id = await _ensure_incident(session, organization=org_a, title=INCIDENT_A_TITLE)
        incident_b_id = await _ensure_incident(session, organization=org_b, title=INCIDENT_B_TITLE)
        # `_ensure_incident` leaves the tenant context on the last organization
        # it touched, so set it explicitly before each org-scoped read.
        await set_tenant_context(session, org_a.id)
        project_a = await tenancy_repository.get_default_project(session, org_a.id)
        await set_tenant_context(session, org_b.id)
        project_b = await tenancy_repository.get_default_project(session, org_b.id)
        assert project_a is not None and project_b is not None
        project_a_id, project_b_id = project_a.id, project_b.id
    async with session_scope() as session:
        await _ensure_timeline_entry(
            session, organization=org_a, incident_id=incident_a_id, marker=TIMELINE_A_MARKER
        )
    async with session_scope() as session:
        await _ensure_timeline_entry(
            session, organization=org_b, incident_id=incident_b_id, marker=TIMELINE_B_MARKER
        )

    # --- connector_configs / incident_timeline: unscoped reads per tenant.
    for table, reader, own_a, own_b in (
        ("connector_configs", _unscoped_connector_markers, CONNECTOR_A_MARKER, CONNECTOR_B_MARKER),
        ("incident_timeline", _unscoped_timeline_markers, TIMELINE_A_MARKER, TIMELINE_B_MARKER),
    ):
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            seen_as_a = await reader(session)
        async with session_scope() as session:
            await set_tenant_context(session, org_b.id)
            seen_as_b = await reader(session)
        _check(
            seen_as_a == {own_a},
            f"{table}: Org A, running a query with NO organization_id filter, "
            f"sees exactly its own row and not Org B's (saw {sorted(seen_as_a)})",
        )
        _check(
            seen_as_b == {own_b},
            f"{table}: Org B, running the same unscoped query, sees exactly its "
            f"own row and not Org A's (saw {sorted(seen_as_b)})",
        )
        _check(
            await _without_tenant_context(reader) == set(),
            f"{table}: a session with NO tenant context sees neither "
            "organization's row (fail-closed)",
        )

    actor_a = _investigator(org_a.id, permissions={"incident:read"})
    actor_a_no_read = _investigator(org_a.id, permissions={"incident:write", "tenancy:manage"})

    # --- live credential resolution returns the correct organization's secret.
    async with session_scope() as session:
        await set_tenant_context(session, org_a.id)
        resolved_a = await tenancy_service.resolve_live_evidence_connectors(
            session,
            actor_a,
            org_a.id,
            incident_organization_id=org_a.id,
            incident_project_id=project_a_id,
            sources={"github"},
        )
    tokens_a = {c.reveal_token() for c in resolved_a.credentials}
    _check(
        TOKEN_A in tokens_a
        and TOKEN_B not in tokens_a
        and all(c.organization_id == org_a.id for c in resolved_a.credentials),
        "live credentials: Org A's investigator resolves Org A's decrypted credential "
        "(envelope round trip) and never Org B's",
    )

    async with session_scope() as session:
        await set_tenant_context(session, org_b.id)
        resolved_b = await tenancy_service.resolve_live_evidence_connectors(
            session,
            _investigator(org_b.id, permissions={"incident:read"}),
            org_b.id,
            incident_organization_id=org_b.id,
            incident_project_id=project_b_id,
            sources={"github"},
        )
    tokens_b = {c.reveal_token() for c in resolved_b.credentials}
    _check(
        TOKEN_B in tokens_b and TOKEN_A not in tokens_b,
        "live credentials: Org B's investigator resolves Org B's credential and never Org A's",
    )

    async with session_scope() as session:
        await set_tenant_context(session, org_b.id)  # WRONG tenant context for actor A
        mismatched = await tenancy_service.resolve_live_evidence_connectors(
            session,
            actor_a,
            org_a.id,
            incident_organization_id=org_a.id,
            incident_project_id=project_a_id,
            sources={"github"},
        )
    _check(
        mismatched.credentials == [],
        "live credentials: with the session's tenant context set to Org B, "
        "Org A's actor gets no connectors at all (RLS hides them even though "
        "the application-level organization filter asked for Org A)",
    )

    async def resolve_foreign_incident() -> None:
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            await tenancy_service.resolve_live_evidence_connectors(
                session,
                actor_a,
                org_a.id,
                incident_organization_id=org_b.id,  # an incident from the other organization
                incident_project_id=project_b_id,
                sources={"github"},
            )

    _check(
        await _raises(PermissionDeniedError, resolve_foreign_incident),
        "live credentials: Org A's actor is refused when the incident belongs to Org B",
    )

    async def resolve_without_read() -> None:
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            await tenancy_service.resolve_live_evidence_connectors(
                session,
                actor_a_no_read,
                org_a.id,
                incident_organization_id=org_a.id,
                incident_project_id=project_a_id,
                sources={"github"},
            )

    _check(
        await _raises(PermissionDeniedError, resolve_without_read),
        "live credentials: an actor without incident:read is refused even in its own organization",
    )

    # --- incident read / timeline write protection under real RLS.
    async def read_foreign_incident() -> None:
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            await incidents_service.get_incident(session, actor_a, org_a.id, incident_b_id)

    _check(
        await _raises(NotFoundError, read_foreign_incident),
        "get_incident: Org A's actor cannot read Org B's incident (not found, not 'forbidden')",
    )

    def _poisoned_result(marker: str) -> InvestigationResult:
        return InvestigationResult(
            evidence=[], hypotheses=[], suggested_owner_team=None, suggested_next_steps=[marker]
        )

    attack_marker = "RLS-PROOF-REJECTED-WRITE"

    async def write_to_foreign_incident() -> None:
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            await incidents_service.record_investigation_result(
                session, actor_a, org_a.id, incident_b_id, _poisoned_result(attack_marker)
            )

    _check(
        await _raises(NotFoundError, write_to_foreign_incident),
        "record_investigation_result: Org A's actor cannot write to Org B's incident",
    )

    async def write_without_read() -> None:
        async with session_scope() as session:
            await set_tenant_context(session, org_a.id)
            await incidents_service.record_investigation_result(
                session, actor_a_no_read, org_a.id, incident_a_id, _poisoned_result(attack_marker)
            )

    _check(
        await _raises(PermissionDeniedError, write_without_read),
        "record_investigation_result: an actor without incident:read cannot write to "
        "its own organization's incident",
    )

    async def count_attack_rows() -> set[str]:
        found: set[str] = set()
        for organization in (org_a, org_b):
            async with session_scope() as session:
                await set_tenant_context(session, organization.id)
                result = await session.execute(
                    text(
                        "SELECT 1 FROM incident_timeline "
                        "WHERE event_data->'suggested_next_steps'->>0 = :marker"
                    ),
                    {"marker": attack_marker},
                )
                if result.first() is not None:
                    found.add(str(organization.id))
        return found

    _check(
        await count_attack_rows() == set(),
        "record_investigation_result: neither rejected write left a timeline row "
        "behind in either organization",
    )


async def _clear_context(session) -> None:
    """Emulate a fresh request: no tenant context set (empty setting)."""
    await session.execute(text("SELECT set_config('app.current_organization_id', '', true)"))


async def _current_context(session) -> str | None:
    value = (
        await session.execute(text("SELECT current_setting('app.current_organization_id', true)"))
    ).scalar_one()
    return value or None


async def _attempt(session, operation):
    """Run `operation()` inside a SAVEPOINT. Returns (True, value) or
    (False, exception): a failed (RLS-rejected) statement aborts a Postgres
    transaction until rolled back to the savepoint, so each scenario needs one
    to leave the others runnable.
    """
    try:
        async with session.begin_nested():
            return True, await operation()
    except Exception as exc:  # noqa: BLE001 -- reported as a failed check
        return False, exc


def _error_summary(exc: BaseException) -> str:
    """Type and first line of the DB error only -- never SQL parameters."""
    orig = getattr(exc, "orig", exc)
    first = (str(orig).strip().splitlines() or [""])[0][:200]
    return f"{type(orig).__name__}: {first}"


async def _organization_creation_checks(org_a: Organization) -> None:
    """Real-Postgres checks of organization creation, signup role assignment
    and returning-SSO-user resolution under RLS, as `ekip_app`.

    ALL of it runs inside ONE transaction on ONE session that never commits
    and is rolled back at the end (each scenario in a SAVEPOINT); a fresh
    session then verifies that no row was left behind. No persistent writes.
    """
    print("\n--- Organization creation under RLS (single rolled-back transaction) ---")
    tag = uuid.uuid4().hex[:8]
    async with session_scope() as s:
        orgs_before = (await s.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
        users_before = (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()

    caller = _investigator(org_a.id, permissions={"tenancy:manage"})
    session = async_session_factory()
    try:
        await session.execute(text("SELECT 1"))  # begin the one outer transaction

        # 1. Authenticated creation (what POST /organizations does).
        async def authenticated():
            await set_tenant_context(session, org_a.id)  # set per request by get_current_identity
            created = await tenancy_service.create_organization(
                session,
                OrganizationCreate(
                    name="RLS Verify Create (auth)", slug=f"rls-verify-create-auth-{tag}"
                ),
                actor=caller,
            )
            context_after = await _current_context(session)
            audit_rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs WHERE organization_id = :o "
                        "AND action = 'organization.create' AND resource_id = :r"
                    ),
                    {"o": str(org_a.id), "r": str(created.id)},
                )
            ).scalar_one()
            await set_tenant_context(session, created.id)
            projects = (
                await session.execute(
                    text("SELECT count(*) FROM projects WHERE organization_id = :o"),
                    {"o": str(created.id)},
                )
            ).scalar_one()
            return context_after, audit_rows, projects

        ok, result = await _attempt(session, authenticated)
        _check(
            ok,
            "create_organization (authenticated, as the REST route does): succeeds under RLS"
            + ("" if ok else f" -- {_error_summary(result)}"),
        )
        if ok:
            context_after, audit_rows, projects = result
            _check(
                context_after == str(org_a.id),
                "create_organization (authenticated): the caller's tenant context is restored "
                "afterwards (the request does not stay scoped to the new organization)",
            )
            _check(
                audit_rows == 1,
                "create_organization (authenticated): the audit row for the CALLER's "
                f"organization was written (rows found: {audit_rows})",
            )
            _check(projects == 1, "create_organization (authenticated): the new organization's "
                   "default project exists under its own tenant context")

        # 2. Unauthenticated creation (signup / SSO / bootstrap): no actor, no prior context.
        async def unauthenticated():
            await _clear_context(session)
            created = await tenancy_service.create_organization(
                session,
                OrganizationCreate(
                    name="RLS Verify Create (anon)", slug=f"rls-verify-create-anon-{tag}"
                ),
                actor=None,
            )
            context_after = await _current_context(session)
            projects = (
                await session.execute(
                    text("SELECT count(*) FROM projects WHERE organization_id = :o"),
                    {"o": str(created.id)},
                )
            ).scalar_one()
            return created.id, context_after, projects

        ok, result = await _attempt(session, unauthenticated)
        _check(
            ok,
            "create_organization (unauthenticated, no prior context): succeeds under RLS"
            + ("" if ok else f" -- {_error_summary(result)}"),
        )
        if ok:
            created_id, context_after, projects = result
            _check(
                projects == 1 and context_after == str(created_id),
                "create_organization (unauthenticated): default project created and the new "
                "organization's context is left set, as signup expects",
            )

        # 3. Real signup -> email verification -> activation, under RLS. Signup itself
        #    must create nothing and issue no session; consuming the token must create
        #    the organization, the verified user and the admin role assignment.
        async def signup():
            await _clear_context(session)
            email = f"rls-verify-create-{tag}@example.invalid"
            slug = f"rls-verify-create-signup-{tag}"
            delivery = await auth_service.signup(
                session,
                SignupRequest(
                    email=email,
                    password="Rls-Verify-Pass-123",
                    display_name="RLS Verify Create",
                    organization_name="RLS Verify Create (signup)",
                    organization_slug=slug,
                ),
            )
            assert delivery is not None, "signup produced no verification delivery"
            before = (
                await session.execute(
                    text("SELECT (SELECT count(*) FROM organizations WHERE slug = :s), "
                         "(SELECT count(*) FROM users WHERE email = :e)"),
                    {"s": slug, "e": email},
                )
            ).one()
            assert tuple(before) == (0, 0), f"signup created an account before verification: {before}"
            await email_verification.verify_token(session, delivery.raw_token)
            roles = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM user_roles WHERE organization_id = "
                        "current_setting('app.current_organization_id', true)::uuid"
                    )
                )
            ).scalar_one()
            verified = (
                await session.execute(
                    text("SELECT email_verified_at IS NOT NULL FROM users WHERE email = :e"),
                    {"e": email},
                )
            ).scalar_one()
            return bool(verified), roles

        ok, result = await _attempt(session, signup)
        _check(
            ok and result[0] and result[1] == 1,
            "signup + verify: nothing exists until the token is consumed, then organization, "
            "verified user and admin role assignment (user_roles) all succeed under RLS"
            + ("" if ok else f" -- {_error_summary(result)}"),
        )

        # 4. A returning SSO user resolves their existing mapping with NO prior context.
        async def sso_returning_user():
            await _clear_context(session)
            created = await tenancy_service.create_organization(
                session,
                OrganizationCreate(
                    name="RLS Verify Create (sso)", slug=f"rls-verify-create-sso-{tag}"
                ),
                actor=None,
            )
            sso_email = f"rls-verify-create-sso-{tag}@example.invalid"
            user_id = await users_service.get_or_create_user(
                session,
                email=sso_email,
                display_name="SSO User",
                email_verified_via="operator_attestation",
            )
            await auth_repository.insert_external_identity_mapping(
                session, organization_id=created.id, user_id=user_id, idp_subject=f"sub-{tag}"
            )
            # A returning SSO user is a current MEMBER (provisioning assigned a role); the SSO
            # login now refuses an identity link whose user holds no role in the organization.
            await set_tenant_context(session, created.id)
            await users_service.assign_role(
                session,
                user_id=user_id,
                organization_id=created.id,
                role_id=await users_service.ensure_admin_role(session),
            )
            await _clear_context(session)
            resolved = await auth_service._resolve_or_provision_user(
                session,
                organization_id=created.id,
                idp_subject=f"sub-{tag}",
                idp_claims=VerifiedIdPClaims(
                    sub=f"sub-{tag}", email=sso_email, email_verified=True
                ),
            )
            return resolved == user_id

        ok, result = await _attempt(session, sso_returning_user)
        _check(
            ok and result is True,
            "SSO provisioning: a returning user's existing mapping is resolved even when no "
            "tenant context was set beforehand"
            + ("" if ok else f" -- {_error_summary(result)}"),
        )
    finally:
        await session.rollback()  # the ONLY way this transaction ends: nothing is committed
        await session.close()

    async with session_scope() as s:
        leftover_orgs = (
            await s.execute(
                text("SELECT count(*) FROM organizations WHERE slug LIKE 'rls-verify-create-%'")
            )
        ).scalar_one()
        leftover_users = (
            await s.execute(
                text("SELECT count(*) FROM users WHERE email LIKE 'rls-verify-create-%'")
            )
        ).scalar_one()
        orgs_after = (await s.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
        users_after = (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()
    unchanged = orgs_after == orgs_before and users_after == users_before
    _check(
        leftover_orgs == 0 and leftover_users == 0 and unchanged,
        "organization-creation checks left nothing behind (transaction rolled back: "
        f"organizations {orgs_before}->{orgs_after}, users {users_before}->{users_after})",
    )


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

    allowed, host = _extended_checks_allowed()
    if allowed:
        # EKIP_RLS_VERIFY_ONLY=org_creation runs only the (rolled-back, no persistent
        # writes) organization-creation checks, skipping the extended section that
        # seeds connector/timeline rows.
        if os.environ.get("EKIP_RLS_VERIFY_ONLY") != "org_creation":
            await _extended_checks(org_a, org_b)
        await _organization_creation_checks(org_a)
    else:
        print(
            f"\n[SKIP] extended connector/timeline/live-credential checks: database host "
            f"{host!r} is not loopback. These checks write rows, so they only run against "
            "a disposable local database (set EKIP_RLS_VERIFY_EXTENDED=1 to force)."
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
