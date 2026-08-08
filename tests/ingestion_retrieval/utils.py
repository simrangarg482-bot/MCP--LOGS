"""Shared utilities for the ingestion & retrieval RAG test harness.

Imports EKIP's REAL, UNMODIFIED internals directly (no new app code, no
edits to anything under `app/`) for the two things no REST/MCP endpoint
covers today (both confirmed by direct code reading, not assumed):

1. Bootstrapping a first identity/organization/token. There is no public
   self-service signup endpoint in EKIP (every tenancy-admin endpoint
   requires an already-authenticated identity) -- this generalizes the same
   real, existing pattern `scripts/seed_test_organization.py` already uses
   (direct calls to `core.tenancy.service.create_organization`,
   `core.users.service.get_or_create_user`/`assign_role`,
   `core.auth.service._issue_session`). A token minted this way is a real,
   normally-signed EKIP session token -- indistinguishable to any endpoint
   that verifies it from one issued by a genuine login.

2. Triggering an ingestion sync. `app.ingestion.service.run_ingestion_job`
   has no REST or MCP caller anywhere in this codebase -- confirmed by
   grepping `app/api/` and `app/mcp/` directly. Production only runs it from
   the arq worker (`app/ingestion/workers/tasks.py`) or an hourly cron. This
   harness calls the same real, unmodified function directly, exactly the
   way that worker does, since there is no REST substitute to call instead.

Every other operation in this harness goes through EKIP's real REST API
over HTTP (registering connectors, asking questions) -- see each numbered/
named script's own docstring for exactly which calls are REST vs. direct.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.auth.service import _issue_session  # noqa: E402
from app.core.exceptions import ConflictError  # noqa: E402
from app.core.tenancy import repository as tenancy_repository  # noqa: E402
from app.core.tenancy import service as tenancy_service  # noqa: E402
from app.core.tenancy.schemas import Organization, OrganizationCreate  # noqa: E402
from app.core.users import service as users_service  # noqa: E402
from app.database.models.core_models import Permission, Role, RolePermission  # noqa: E402
from app.database.session import engine, session_scope, set_tenant_context  # noqa: E402
from app.shared.schemas import Identity  # noqa: E402

# ---------------------------------------------------------------------------
# One shared, persistent event loop for the whole process.
#
# WHY: `app.database.session.engine` is a module-level singleton whose
# pooled asyncpg connections are bound to whichever event loop was running
# when they were opened. Calling `asyncio.run(...)` (which creates AND
# CLOSES a new loop every time) more than once in one process -- exactly
# what this harness does across bootstrap, ingestion-trigger, and
# verification calls -- eventually reuses/closes a connection tied to an
# already-closed loop, raising "RuntimeError: Event loop is closed". This
# was a real, empirically-observed bug in a sibling harness
# (scripts/realworld_onboarding/common/bootstrap.py) fixed the same way;
# applying the same fix here from the start rather than re-discovering it.
# ---------------------------------------------------------------------------
_shared_loop: asyncio.AbstractEventLoop | None = None


def run_async(coro: Awaitable[Any]) -> Any:
    global _shared_loop
    if _shared_loop is None or _shared_loop.is_closed():
        _shared_loop = asyncio.new_event_loop()
    return _shared_loop.run_until_complete(coro)


def dispose_shared_loop() -> None:
    """Optional, best-effort cleanup -- see common/bootstrap.py's identical
    helper in the sibling harness for why this exists (cosmetic only, never
    affects any PASS/FAIL result already printed)."""
    global _shared_loop
    if _shared_loop is not None and not _shared_loop.is_closed():
        _shared_loop.run_until_complete(engine.dispose())
        _shared_loop.close()
    _shared_loop = None


# ---------------------------------------------------------------------------
# Bootstrap: one admin identity + organization, real token.
# ---------------------------------------------------------------------------

_TENANCY_MANAGE = "tenancy:manage"
_ALL_PERMISSION_CODES = [
    "tenancy:manage",
    "incident:write",
    "postmortem:write",
    "postmortem:approve",
    "knowledge:review",
    "observability:read",
]


async def _ensure_admin_role(session) -> Role:
    result = await session.execute(select(Permission).where(Permission.code.in_(_ALL_PERMISSION_CODES)))
    existing = {row.code: row for row in result.scalars().all()}
    for code in _ALL_PERMISSION_CODES:
        if code not in existing:
            row = Permission(code=code, description=f"Seeded by ingestion_retrieval harness: {code}")
            session.add(row)
            existing[code] = row
    await session.flush()

    result = await session.execute(select(Role).where(Role.name == "rag_harness_admin"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name="rag_harness_admin", description="Seeded by ingestion_retrieval harness")
        session.add(role)
        await session.flush()

    granted = await session.execute(select(RolePermission.permission_id).where(RolePermission.role_id == role.id))
    already_granted = set(granted.scalars().all())
    for permission in existing.values():
        if permission.id not in already_granted:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    await session.flush()
    return role


async def _bootstrap_admin(*, org_name: str, org_slug: str, email: str, display_name: str) -> dict:
    async with session_scope() as session:
        try:
            organization = await tenancy_service.create_organization(
                session, OrganizationCreate(name=org_name, slug=org_slug)
            )
        except ConflictError:
            row = await tenancy_repository.get_organization_by_slug(session, org_slug)
            if row is None:
                raise
            organization = Organization.model_validate(row)

        role = await _ensure_admin_role(session)
        user_id = await users_service.get_or_create_user(session, email=email, display_name=display_name)

        await set_tenant_context(session, organization.id)
        await users_service.assign_role(
            session, user_id=user_id, organization_id=organization.id, role_id=role.id
        )
        default_project = await tenancy_service.get_default_project(
            session, Identity.for_agent("rag_harness", organization.id), organization.id
        )
        tokens = await _issue_session(session, user_id=user_id, organization_id=organization.id, family_id=uuid.uuid4())

    return {
        "organization_id": str(organization.id),
        "organization_slug": organization.slug,
        "project_id": str(default_project.id),
        "user_id": str(user_id),
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
    }


def bootstrap_admin_sync(*, org_name: str, org_slug: str, email: str, display_name: str) -> dict:
    """See module docstring, point 1. Idempotent: re-running against an
    already-created org_slug reuses the existing organization/user."""
    return run_async(_bootstrap_admin(org_name=org_name, org_slug=org_slug, email=email, display_name=display_name))


# ---------------------------------------------------------------------------
# HTTP client for the REAL REST API.
# ---------------------------------------------------------------------------


class ConnectionRefused(RuntimeError):
    pass


@dataclass
class StepResult:
    name: str
    passed: bool
    elapsed_seconds: float
    detail: str = ""


_RESULTS: list[StepResult] = []


def reset_results() -> None:
    _RESULTS.clear()


def get_results() -> list[StepResult]:
    """A snapshot copy of every result recorded since the last
    `reset_results()` -- used by run_rag_validation.py to pull each phase's
    detailed results out before the NEXT phase's own `reset_results()` call
    clears them."""
    return list(_RESULTS)


def record_result(name: str, passed: bool, *, elapsed_seconds: float = 0.0, detail: str = "") -> None:
    _RESULTS.append(StepResult(name=name, passed=passed, elapsed_seconds=elapsed_seconds, detail=detail))


def print_summary(title: str = "SUMMARY") -> bool:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)
    all_passed = True
    for result in _RESULTS:
        status = "PASS" if result.passed else "FAIL"
        if not result.passed:
            all_passed = False
        line = f"{result.name:<45} {status:>6}  ({result.elapsed_seconds:.2f}s)"
        if result.detail and not result.passed:
            line += f"   {result.detail}"
        print(line)
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r.passed)
    print("=" * 64)
    print(f"{passed}/{total} checks passed.")
    print("=" * 64)
    return all_passed


@dataclass
class ApiClient:
    base_url: str
    timeout_seconds: float
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict | None = None,
        redact_keys: tuple[str, ...] = ("credential_ref", "access_token", "refresh_token"),
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        safe_body = _redact_dict(json_body, redact_keys) if json_body else None
        print(f"  -> {method} {self.base_url}{path}")
        if safe_body is not None:
            print(f"     payload: {safe_body}")
        start = time.monotonic()
        try:
            response = self._client.request(method, path, json=json_body, headers=headers)
        except httpx.ConnectError as exc:
            raise ConnectionRefused(f"Could not reach {self.base_url} -- is the API server running?") from exc
        elapsed = time.monotonic() - start
        print(f"     status: {response.status_code}   elapsed: {elapsed:.2f}s")
        try:
            print(f"     response: {_truncate(response.json())}")
        except ValueError:
            print(f"     response (non-JSON, truncated): {response.text[:300]}")
        return response

    def close(self) -> None:
        self._client.close()


def _redact_dict(data: dict, keys: tuple[str, ...]) -> dict:
    return {k: ("<redacted>" if k in keys else v) for k, v in data.items()}


def _truncate(value: Any, limit: int = 600) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def redact_secret(value: str | None) -> str:
    if not value:
        return "<not set>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# Connector fetch loop -- shared by test_connectors.py and
# test_ingestion_pipeline.py, following the Connector Protocol exactly
# (app/ingestion/connectors/base.py), same authenticate -> fetch_batch (loop
# while has_more) -> normalize -> close sequence `_execute_ingestion_job`
# itself runs, just without the DB job-tracking wrapper around it.
# ---------------------------------------------------------------------------


async def fetch_all(connector, resolved_config, *, max_batches: int = 5) -> tuple[list, list, int]:
    """Returns (raw_items, normalized_documents, batch_count).

    `max_batches` bounds a real-source pull to a sane sample size for a test
    run (a real Slack channel or GitHub repo could otherwise page for a very
    long time) -- this is a harness-side safety limit, not a change to the
    connector's own pagination behavior.
    """
    client = await connector.authenticate(resolved_config)
    raw_items: list = []
    normalized: list = []
    cursor: str | None = None
    batches = 0
    try:
        while True:
            fetch_result = await connector.fetch_batch(client, since=None, cursor=cursor)
            batches += 1
            raw_items.extend(fetch_result.items)
            for item in fetch_result.items:
                normalized.append(connector.normalize(item))
            if not fetch_result.has_more or batches >= max_batches:
                break
            cursor = fetch_result.next_cursor
    finally:
        await connector.close(client)
    return raw_items, normalized, batches


def fetch_all_sync(connector, resolved_config, *, max_batches: int = 5) -> tuple[list, list, int]:
    return run_async(fetch_all(connector, resolved_config, max_batches=max_batches))
