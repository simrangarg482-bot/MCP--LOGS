"""End-to-end manual smoke test through Milestone 6: seeds a throwaway
organization/user/permission-catalog bootstrap (there is no signup/SSO flow
built yet that could do this for us -- core/auth's OIDC login needs a real
IdP), registers your Slack and/or GitHub connectors from `.env`, runs a real
ingestion job for each, then asks a real question via
`agents.service.answer_question` and prints the result.

Not a pytest test, same reasoning as `test_connectors.py`: real DB writes,
real OpenAI spend, real network calls. Lives in scripts/, not tests/.

Unlike `test_connectors.py`, this needs the *full* project environment (
SQLAlchemy, LangGraph, sentence-transformers, ...), not `scripts/requirements.txt`'s
minimal connector-only set -- run this with the same environment you run the
app itself with (`uv sync`, or whatever installed `pyproject.toml`'s
dependencies).

Prerequisites (one-time, outside this script):
  1. `alembic upgrade head` run against your Neon DB.
  2. `CREATE EXTENSION IF NOT EXISTS vector;` run once against your Neon DB
     (e.g. via Neon's SQL editor, or `psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"`).
  3. `.env` (repo root) has DATABASE_URL, REDIS_URL, OPENAI_API_KEY,
     JWT_SECRET_KEY, and at least one of the EKIP_TEST_SLACK_*/
     EKIP_TEST_GITHUB_* pairs from `test_connectors.py`.

What this script seeds, idempotently (safe to re-run):
  - The permission catalog (`tenancy:manage`, `incident:write`,
    `postmortem:write`, `postmortem:approve`) and one "admin" role granting
    all of them. Nowhere else in the codebase seeds this catalog --
    `core.tenancy.service`'s own module docstring calls it "a data migration
    concern, not something this module manages" -- so a throwaway bootstrap
    script is the right place for it until a real seed migration exists.
  - One organization (slug "milestone6-test") with its auto-created default
    project, one user, and that user holding the "admin" role in that
    organization.
  - One connector_config per credential pair present in the environment
    (skipped if a connector for that source is already registered).

Run: python scripts/test_milestone6.py "<your question>"
(a default question is used if you don't pass one)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import service as agents_service
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import ConnectorConfigCreate, Organization, OrganizationCreate
from app.core.users import service as users_service
from app.database.models.core_models import Permission, Role, RolePermission
from app.database.session import session_scope
from app.ingestion import service as ingestion_service
from app.shared.schemas import Identity

load_dotenv()

_ORG_SLUG = "milestone6-test"
_ORG_NAME = "Milestone 6 Test Org"
_ADMIN_EMAIL = "milestone6-test@example.com"
_ADMIN_DISPLAY_NAME = "Milestone 6 Test Admin"
_ADMIN_ROLE_NAME = "admin"

# Every permission code referenced anywhere in core/ today -- see this
# script's module docstring on why seeding this catalog lives here rather
# than in a real migration.
_PERMISSION_CATALOG = {
    "tenancy:manage": "Manage organization settings, projects, connectors, SSO.",
    "incident:write": "Create and update incidents.",
    "postmortem:write": "Edit postmortem drafts.",
    "postmortem:approve": "Approve postmortems.",
}


async def _seed_permission_catalog(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Idempotently ensure every code in `_PERMISSION_CATALOG` exists as a
    `permissions` row. Returns `{code: id}`.
    """
    ids: dict[str, uuid.UUID] = {}
    for code, description in _PERMISSION_CATALOG.items():
        result = await session.execute(select(Permission).where(Permission.code == code))
        row = result.scalar_one_or_none()
        if row is None:
            row = Permission(code=code, description=description)
            session.add(row)
            await session.flush()
        ids[code] = row.id
    return ids


async def _seed_admin_role(session: AsyncSession, permission_ids: dict[str, uuid.UUID]) -> uuid.UUID:
    """Idempotently ensure an `"admin"` role exists, granting every
    permission in `permission_ids`. Returns the role's id.
    """
    result = await session.execute(select(Role).where(Role.name == _ADMIN_ROLE_NAME))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name=_ADMIN_ROLE_NAME, description="Full-access bootstrap role for testing.")
        session.add(role)
        await session.flush()

    for permission_id in permission_ids.values():
        existing = await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == permission_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    await session.flush()
    return role.id


async def _get_or_create_organization(session: AsyncSession) -> Organization:
    existing = await tenancy_repository.get_organization_by_slug(session, _ORG_SLUG)
    if existing is not None:
        return Organization.model_validate(existing)
    return await tenancy_service.create_organization(
        session, OrganizationCreate(name=_ORG_NAME, slug=_ORG_SLUG)
    )


async def _register_connectors(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[uuid.UUID]:
    """Register a connector_config per credential pair found in the
    environment, skipping any source already registered for this
    organization (safe to re-run).
    """
    existing = await tenancy_service.list_connectors(session, actor, organization_id)
    existing_sources = {connector.source for connector in existing}
    connector_config_ids = [connector.id for connector in existing]

    slack_token = os.environ.get("EKIP_TEST_SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("EKIP_TEST_SLACK_CHANNEL_ID")
    if slack_token and slack_channel and "slack" not in existing_sources:
        config = await tenancy_service.register_connector(
            session,
            actor,
            organization_id,
            ConnectorConfigCreate(
                source="slack", credential_ref=slack_token, config={"channels": [slack_channel]}
            ),
        )
        connector_config_ids.append(config.id)
        print(f"Registered Slack connector: {config.id}")

    github_token = os.environ.get("EKIP_TEST_GITHUB_TOKEN")
    github_repo = os.environ.get("EKIP_TEST_GITHUB_REPO")
    if github_token and github_repo and "github" not in existing_sources:
        github_ref = os.environ.get("EKIP_TEST_GITHUB_REF", "main")
        config = await tenancy_service.register_connector(
            session,
            actor,
            organization_id,
            ConnectorConfigCreate(
                source="github",
                credential_ref=github_token,
                config={"repos": [{"repo": github_repo, "ref": github_ref}]},
            ),
        )
        connector_config_ids.append(config.id)
        print(f"Registered GitHub connector: {config.id}")

    if not connector_config_ids:
        print(
            "No connector credentials found in the environment "
            "(EKIP_TEST_SLACK_*/EKIP_TEST_GITHUB_*) and none already "
            "registered -- nothing to ingest."
        )
    return connector_config_ids


async def bootstrap() -> tuple[Identity, uuid.UUID, list[uuid.UUID]]:
    """Seed everything and return `(actor, organization_id, connector_config_ids)`."""
    async with session_scope() as session:
        permission_ids = await _seed_permission_catalog(session)
        role_id = await _seed_admin_role(session, permission_ids)

        org = await _get_or_create_organization(session)

        user_id = await users_service.get_or_create_user(
            session, email=_ADMIN_EMAIL, display_name=_ADMIN_DISPLAY_NAME
        )
        await users_service.assign_role(
            session, user_id=user_id, organization_id=org.id, role_id=role_id
        )

        actor = await users_service.resolve_identity(session, user_id, org.id)
        connector_config_ids = await _register_connectors(session, actor, org.id)

    return actor, org.id, connector_config_ids


async def run_ingestion(connector_config_ids: list[uuid.UUID]) -> None:
    for connector_config_id in connector_config_ids:
        async with session_scope() as session:
            job = await ingestion_service.run_ingestion_job(session, connector_config_id)
            print(
                f"Ingestion job {job.id}: status={job.status}, "
                f"documents_processed={job.documents_processed}"
                + (f", failed_stage={job.failed_stage}" if job.failed_stage else "")
            )


async def ask(actor: Identity, query: str) -> None:
    async with session_scope() as session:
        response = await agents_service.answer_question(session, query, None, actor)

    print("\n--- AskResponse ---")
    print(f"route_taken: {response.route_taken}")
    print(f"confidence:  {response.confidence:.3f}")
    if response.route_taken == "answer":
        print(f"answer: {response.answer}")
        print(f"citations ({len(response.citations)}):")
        for citation in response.citations:
            print(
                f"  - document={citation.document_id} chunk={citation.chunk_id} "
                f"url={citation.source_url}"
            )
            print(f"    excerpt: {citation.excerpt}")
    else:
        print(f"investigation: {response.investigation}")


async def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "What is this project about?"

    print("Bootstrapping organization/user/connectors...")
    actor, organization_id, connector_config_ids = await bootstrap()
    print(f"Organization: {organization_id}")
    print(f"Actor: {actor.audit_tag}, permissions={sorted(actor.permissions)}")

    if connector_config_ids:
        print("\nRunning ingestion...")
        await run_ingestion(connector_config_ids)

    print(f"\nAsking: {query!r}")
    await ask(actor, query)


if __name__ == "__main__":
    asyncio.run(main())
