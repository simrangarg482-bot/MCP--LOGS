"""Public interface for core/privacy -- data-subject deletion.

Owned by: core/privacy. Business rules and ownership decisions live here;
raw SQL lives in repository.py; HTTP concerns live in app/api.

Transaction model: the session is a parameter, not created here -- the same
convention every other `core/*` service follows, and it matters more here
than usual. The entire execution runs inside the caller's transaction, so a
failure that propagates rolls the whole deletion back rather than leaving a
user half-deleted. Steps that fail *without* propagating (recorded as failed
in the result) are the deliberate exception -- see `execute_user_data_deletion`.

WHY SYNCHRONOUS, NOT AN arq JOB
    A deletion request is not queued, and there is no `deletion_requests`
    table. This is a deliberate, evidence-based choice rather than a
    shortcut: user-scoped deletion touches only small, bounded, per-user row
    sets -- sessions, role/project memberships, identity mappings, that
    user's own agent-execution rows, one `users` row, and their invitations.
    It notably does NOT touch documents, chunks, or embeddings, because those
    are organization-owned (see `docs/DATA_LIFECYCLE.md`) and are retained.
    That is a handful of indexed single-statement writes, comfortably inside
    one request and one transaction -- and doing it synchronously buys real
    safety properties a queued job would give up: atomicity (all-or-nothing
    within the transaction), an immediately observable result, and no window
    where a "pending" request is visible but unapplied.

    ORGANIZATION deletion is the case that genuinely needs the job model and
    the worker: it would cascade into documents, all three chunk tables, and
    every embedding, at a scale no request should hold a transaction open
    for. It is deliberately not implemented -- see `DeletionScope` and
    `docs/DATA_LIFECYCLE.md`'s "Deferred: pending product decision".

AUTHORIZATION
    `tenancy:manage` within the caller's own organization, enforced through
    the existing `core.users.service.require_permission` -- the same
    permission and the same helper every other admin-scoped operation in
    this codebase uses (`configure_sso`, `create_project`,
    `revoke_all_sessions`'s API caller). No new permission code and no
    second authorization concept is introduced. Cross-tenant deletion is
    structurally impossible: every repository statement is scoped by
    `organization_id` as well as `user_id`, and the organization used is
    always `actor.organization_id`, never a caller-supplied value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.auth import service as auth_service
from app.core.exceptions import NotFoundError, ValidationError
from app.core.memory import repository as memory_repository
from app.core.privacy import repository
from app.core.privacy.schemas import (
    DeletionPlan,
    DeletionResult,
    DeletionScope,
    DeletionStatus,
    PlannedStep,
    StepResult,
)
from app.core.users.service import require_permission
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_MANAGE_PERMISSION = "tenancy:manage"


async def plan_user_data_deletion(
    session: AsyncSession,
    actor: Identity,
    user_id: uuid.UUID,
    *,
    scope: DeletionScope = "user_data",
) -> DeletionPlan:
    """Compute -- without mutating anything -- exactly what deleting
    `user_id`'s data within the caller's organization would do.

    This is the dry-run path, and it is the *same* discovery code the
    executing path uses, not a parallel reimplementation: `execute_user_data_
    deletion` calls this first and then acts on the plan it returns. A
    dry-run that ran different code from the real thing would be
    actively misleading.

    Raises `NotFoundError` if no such user row exists at all. Deliberately
    does NOT raise for a user who exists but has no data in this
    organization -- that produces a plan with zero-count steps, which is a
    meaningful (and honest) answer rather than an error.
    """
    _assert_scope_supported(scope)
    require_permission(actor, _MANAGE_PERMISSION)
    organization_id = actor.organization_id

    if not await repository.user_exists(session, user_id):
        raise NotFoundError(
            "No such user.",
            error_code="privacy.user_not_found",
            detail={"user_id": str(user_id)},
        )

    email = await repository.get_user_email(session, user_id)
    invitation_count = (
        await repository.count_invitations_for_email(session, email, organization_id)
        if email is not None
        else 0
    )

    steps = [
        PlannedStep(
            category="sessions",
            action="hard_delete",
            table="refresh_tokens",
            count=await repository.count_refresh_tokens(session, user_id, organization_id),
            rationale=(
                "Ephemeral session state with no audit value; the schema's own FK is "
                "ON DELETE CASCADE. Revoking alone is insufficient -- a revoked row "
                "still carries user_id."
            ),
        ),
        PlannedStep(
            category="organization_roles",
            action="hard_delete",
            table="user_roles",
            count=await repository.count_user_roles(session, user_id, organization_id),
            rationale=(
                "Access grant, not history. Removing it is what actually ends the "
                "person's access to this organization. FK is ON DELETE CASCADE."
            ),
        ),
        PlannedStep(
            category="project_memberships",
            action="hard_delete",
            table="project_memberships",
            count=await repository.count_project_memberships(session, user_id, organization_id),
            rationale=(
                "Project-scoped access grant, same category as organization_roles. "
                "Scoped to this org via its projects, since the table has no "
                "organization_id of its own."
            ),
        ),
        PlannedStep(
            category="sso_identity_links",
            action="hard_delete",
            table="external_identity_mappings",
            count=await repository.count_external_identity_mappings(
                session, user_id, organization_id
            ),
            rationale=(
                "Maps an IdP subject claim to this account; an authentication "
                "artifact carrying a third-party identifier. FK is ON DELETE CASCADE."
            ),
        ),
        PlannedStep(
            category="agent_execution_history",
            action="anonymize",
            table="agent_executions",
            count=await repository.count_agent_executions(session, user_id, organization_id),
            rationale=(
                "Organization-level cost/usage/confidence telemetry that happens to "
                "record who triggered it. user_id is nulled (exactly what the "
                "column's ON DELETE SET NULL already declares); the row is kept so "
                "the org's own usage history is not silently rewritten."
            ),
        ),
        PlannedStep(
            category="user_private_memory",
            action="hard_delete",
            table="agent_memories",
            count=await memory_repository.count_user_scoped(session, user_id, organization_id),
            rationale=(
                "Private to this person by construction (scope='user', "
                "owner_user_id set) and retrievable by nobody else, so there is "
                "no shared value to preserve -- a genuine row DELETE, which "
                "also removes the embedding because it is a column on the same "
                "row (no orphan vector is possible). Project-scoped memories "
                "this person created are deliberately NOT touched: they are "
                "shared with a project, exactly as documents authored by a "
                "departing employee remain organization knowledge."
            ),
        ),
        PlannedStep(
            category="invitations_received",
            action="anonymize",
            table="invitations",
            count=invitation_count,
            rationale=(
                "The only table besides users holding a raw email address. The "
                "address is replaced; the row is kept because it also records who "
                "invited whom (invited_by is ON DELETE RESTRICT) and backs the "
                "one-pending-invite-per-email index."
            ),
        ),
        PlannedStep(
            category="user_record",
            action="anonymize",
            table="users",
            count=1,
            rationale=(
                "Cannot be deleted: incidents.reported_by, postmortems.reviewed_by "
                "and invitations.invited_by are all ON DELETE RESTRICT. Email, "
                "display name and password hash are cleared and is_active set false "
                "-- which also neutralizes every 'user:<uuid>' tagged-actor string "
                "elsewhere, since they all dereference to this row."
            ),
        ),
        PlannedStep(
            category="incidents_reported",
            action="retain",
            table="incidents",
            count=await repository.count_incidents_reported(session, user_id, organization_id),
            rationale=(
                "Organization-owned operational history. reported_by is ON DELETE "
                "RESTRICT -- the schema stating outright that incident history "
                "outlives the individual who filed it. Attribution is neutralized "
                "by anonymizing the user record, not by deleting incidents."
            ),
        ),
        PlannedStep(
            category="audit_trail",
            action="retain",
            table="audit_logs",
            count=None,
            rationale=(
                "Append-only by explicit contract (see AuditLog's docstring: 'no "
                "updates, no deletes, ever'). The actor string is a surrogate "
                "'user:<uuid>' reference that dereferences to the now-anonymized "
                "user row, so the trail stays intact while ceasing to identify "
                "anyone. Rewriting it would violate the table's own contract."
            ),
        ),
        PlannedStep(
            category="organization_knowledge",
            action="retain",
            table="documents / *_chunks / connector_configs",
            count=None,
            rationale=(
                "Organization-owned, never user-owned: documents, chunks and "
                "connector_configs carry organization_id (and project_id) but have "
                "no user_id column at all. A user who configured a connector does "
                "not own the knowledge it ingested."
            ),
        ),
    ]

    return DeletionPlan(
        scope=scope,
        target_user_id=user_id,
        organization_id=organization_id,
        steps=steps,
    )


async def execute_user_data_deletion(
    session: AsyncSession,
    actor: Identity,
    user_id: uuid.UUID,
    *,
    scope: DeletionScope = "user_data",
) -> DeletionResult:
    """Execute the plan from `plan_user_data_deletion`, returning a per-step
    result.

    IDEMPOTENCY. Safe to re-run. Every mutation is a `DELETE/UPDATE ... WHERE`
    that matches zero rows once it has already been applied, so a second run
    reports `rows_affected=0` per step and succeeds. `was_noop` is set when
    the user record was already anonymized on entry -- that is the one signal
    that distinguishes "this run did the work" from "a previous run already
    had", which a bare count of zero cannot express. Note the deliberate
    asymmetry: re-running is *not* treated as an error, because a retry after
    a partial failure is exactly the intended recovery path.

    PARTIAL FAILURE. Each step is executed independently and its outcome
    recorded. If any step raises, that step is marked failed and the
    remaining steps still run -- so one broken step cannot silently prevent
    the rest of a person's data from being cleaned, and the result names
    precisely what is left over. The overall status is then
    `partially_completed` (some succeeded) or `failed` (none did), never
    `completed`. A caller that treats a non-`completed` status as success is
    misusing this function.

    A caveat worth stating plainly: because every step shares the caller's
    transaction, a failure that poisons the transaction (as most database
    errors do) will make subsequent steps fail too, and committing may
    itself fail. The per-step accounting is therefore most useful for
    application-level failures; it is not a claim that this function can
    partially commit inside a poisoned transaction. Retrying the whole
    operation after the underlying cause is fixed is the supported recovery.
    """
    _assert_scope_supported(scope)
    plan = await plan_user_data_deletion(session, actor, user_id, scope=scope)
    organization_id = actor.organization_id

    already_anonymized = await repository.is_user_anonymized(session, user_id)
    email = await repository.get_user_email(session, user_id)

    # Sessions are revoked through the existing, already-tested
    # `core.auth.service.revoke_all_sessions` before the token rows are
    # deleted. Deleting the rows alone would end up in the same place, but
    # going through the real revocation path first keeps a single source of
    # truth for "what revocation means" and leaves its audit/logging
    # behavior intact rather than bypassing it.
    steps: list[StepResult] = []

    async def _run(category: str, action, coroutine_factory) -> None:
        try:
            rows = await coroutine_factory()
            steps.append(
                StepResult(category=category, action=action, succeeded=True, rows_affected=rows)
            )
        except Exception as exc:  # noqa: BLE001 -- one failed step must not abort the rest
            logger.warning(
                "privacy_deletion_step_failed",
                category=category,
                target_user_id=str(user_id),
                organization_id=str(organization_id),
                error=str(exc),
            )
            steps.append(
                StepResult(category=category, action=action, succeeded=False, error=str(exc))
            )

    async def _revoke_then_delete_sessions() -> int:
        await auth_service.revoke_all_sessions(session, user_id, organization_id)
        return await repository.delete_refresh_tokens(session, user_id, organization_id)

    await _run("sessions", "hard_delete", _revoke_then_delete_sessions)
    await _run(
        "organization_roles",
        "hard_delete",
        lambda: repository.delete_user_roles(session, user_id, organization_id),
    )
    await _run(
        "project_memberships",
        "hard_delete",
        lambda: repository.delete_project_memberships(session, user_id, organization_id),
    )
    await _run(
        "sso_identity_links",
        "hard_delete",
        lambda: repository.delete_external_identity_mappings(session, user_id, organization_id),
    )
    await _run(
        "agent_execution_history",
        "anonymize",
        lambda: repository.anonymize_agent_executions(session, user_id, organization_id),
    )
    await _run(
        "user_private_memory",
        "hard_delete",
        lambda: memory_repository.hard_delete_user_scoped(session, user_id, organization_id),
    )
    if email is not None:
        await _run(
            "invitations_received",
            "anonymize",
            lambda: repository.anonymize_invitations_for_email(
                session,
                email,
                organization_id,
                repository.anonymized_email_for(user_id),
            ),
        )
    else:
        # No resolvable email means the user record was already anonymized
        # by a previous run, so there is no raw address left to find
        # invitations by. Recorded as a real, succeeded zero-row step rather
        # than omitted, so the result's step list stays comparable run to run.
        steps.append(
            StepResult(
                category="invitations_received", action="anonymize", succeeded=True, rows_affected=0
            )
        )
    await _run(
        "user_record",
        "anonymize",
        lambda: repository.anonymize_user_record(session, user_id),
    )

    status = _derive_status(steps)
    result = DeletionResult(
        scope=scope,
        target_user_id=user_id,
        organization_id=organization_id,
        status=status,
        steps=steps,
        executed_at=datetime.now(UTC),
        was_noop=already_anonymized,
    )

    # The audit event deliberately records only counts and status -- never
    # the deleted person's email or display name. Writing those into an
    # audit row "for traceability" would preserve exactly the data the
    # operation exists to remove. `target_user_id` is an opaque surrogate
    # key, not personal data, and without it the event could not be
    # correlated to anything.
    await record_audit_event(
        session,
        actor,
        action="privacy.user_data_deleted",
        resource_type="user",
        resource_id=user_id,
        metadata={
            "scope": scope,
            "status": status,
            "hard_deleted_rows": result.hard_deleted_row_count,
            "anonymized_rows": result.anonymized_row_count,
            "failed_steps": [s.category for s in result.failed_steps],
            "was_noop": already_anonymized,
            "retained_categories": [s.category for s in plan.retain_steps],
        },
    )

    logger.info(
        "privacy_user_data_deleted",
        target_user_id=str(user_id),
        organization_id=str(organization_id),
        status=status,
        hard_deleted_rows=result.hard_deleted_row_count,
        anonymized_rows=result.anonymized_row_count,
        was_noop=already_anonymized,
    )
    return result


def _derive_status(steps: list[StepResult]) -> DeletionStatus:
    """`completed` only when every step succeeded. Anything else is named
    honestly -- see `execute_user_data_deletion`'s docstring."""
    if all(step.succeeded for step in steps):
        return "completed"
    if any(step.succeeded for step in steps):
        return "partially_completed"
    return "failed"


def _assert_scope_supported(scope: DeletionScope) -> None:
    """Reject the declared-but-unimplemented scopes explicitly.

    `user_account` and `organization` are part of `DeletionScope`'s
    vocabulary so the API contract is stable, but neither is implemented.
    Failing loudly is the only honest option: silently treating either as
    `user_data` would report a successful "organization deletion" that left
    every document, chunk and embedding in place.
    """
    if scope != "user_data":
        raise ValidationError(
            f"Deletion scope {scope!r} is not implemented.",
            error_code="privacy.scope_not_implemented",
            detail={
                "scope": scope,
                "supported": ["user_data"],
                "reason": (
                    "See docs/DATA_LIFECYCLE.md -- organization deletion requires a "
                    "product decision on knowledge ownership plus background execution, "
                    "and user_account deletion requires deciding whether the users row "
                    "may be removed given its RESTRICT foreign keys."
                ),
            },
        )
