"""Public interface for ingestion/ (PROJECT_PLAN.md section 9.8):
`run_ingestion_job(connector_config_id)`, `reindex(document_id)`,
`get_job_status(job_id)`.

Owned by: ingestion/. Ties together connectors (task #9/#10), the
processing pipeline (task #11), and persistence (this task's repository.py)
into the one job-execution flow. Depends on core/tenancy (reading
`connector_configs` via `ingestion/repository.py`'s direct database read,
and writing sync status back through `core.tenancy.service.update_connector_sync_status`)
and core/users (indirectly, via `Identity.for_agent` -- no permission check
is performed here, see below) -- both undocumented in section 9.8's
dependency list (retrieval/database/shared only), flagged the same way the
connector-config-access gap was flagged and resolved earlier in this
milestone. Also depends on retrieval/ (`retrieval.service.upsert`) to hand
off each processed document's chunks for embedding and storage -- this one
*is* in section 9.8's dependency list.

No `actor: Identity` parameter anywhere in this module's public functions,
matching section 9.8's literal signatures. Ingestion runs as a separate
worker process (section 4.5), triggered by a scheduler/webhook handler, not
synchronously by an end user request -- there is no human caller to
authorize per call the way core/'s user-facing functions require. Every
mutation still gets an actor for audit-tagging purposes: internally, a job
constructs `Identity.for_agent("ingestion_worker", organization_id)`, the
same convenience constructor `core/auth` and `core/incidents` use for
non-interactive callers.

Each processed document's chunks are handed to `retrieval.service.upsert`
inside the same savepoint as the `Document`/`document_metadata` writes, so a
mid-job failure rolls back the chunks along with the row they belong to --
no orphaned embeddings for a document that never successfully persisted.
`_CONTENT_TYPE_TO_COLLECTION` maps `ProcessedDocument.content_type` onto the
`CollectionName` `retrieval/` expects (see that mapping's own comment for
why it lives here rather than in either module it bridges).

Milestone 10 additions (PROJECT_PLAN.md section 12.5/section 10): (1)
`config_row.credential_ref` is decrypted via `app.shared.security` exactly
once per job, immediately before `connector.authenticate()` needs it --
see `_execute_ingestion_job`'s own docstring; (2) every `fetch_batch` call
acquires from two `app.shared.rate_limiter.TokenBucketRateLimiter`
budgets first (per-connector_config and per-organization), closing the gap
`app.ingestion.workers.tasks.scheduled_reconciliation`'s docstring used to
flag as "not attempted here." (Phase 6.5 relocated this shared class from
`app.ingestion.rate_limiter` to `app.shared.rate_limiter` when API-level
rate limiting needed the identical algorithm and `app.api` cannot depend on
`app.ingestion` at all -- see that module's own docstring.)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from opentelemetry import trace
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import service as tenancy_service
from app.database.session import session_scope, set_tenant_context
from app.ingestion import repository
from app.ingestion.connectors.azure_devops import AzureDevOpsConnector
from app.ingestion.connectors.base import Connector
from app.ingestion.connectors.confluence import ConfluenceConnector
from app.ingestion.connectors.github import GitHubConnector
from app.ingestion.connectors.gitlab import GitLabConnector
from app.ingestion.connectors.google_drive import GoogleDriveConnector
from app.ingestion.connectors.jira import JiraConnector
from app.ingestion.connectors.notion import NotionConnector
from app.ingestion.connectors.pagerduty import PagerDutyConnector
from app.ingestion.connectors.runbooks import RunbooksConnector
from app.ingestion.connectors.servicenow import ServiceNowConnector
from app.ingestion.connectors.sharepoint import SharePointConnector
from app.ingestion.connectors.slack import SlackConnector
from app.ingestion.connectors.teams import TeamsConnector
from app.ingestion.processors.pipeline import process_document
from app.ingestion.schemas import ContentType, FetchResult, IngestionJob, ResolvedConnectorConfig
from app.retrieval import service as retrieval_service
from app.retrieval.schemas import CollectionName, UpsertChunk
from app.shared.backoff import full_jitter_backoff_seconds
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.distributed_rate_limiter import AsyncRateLimiter
from app.shared.rate_limiter import TokenBucketRateLimiter
from app.shared.schemas import Identity
from app.shared.security import decrypt_secret, get_kms

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# One connector instance per known source (PROJECT_PLAN.md section 4.2:
# "source_name must match the `source` value used on connector_configs
# rows"). Connectors are stateless between jobs (see each connector's
# module docstring on why fetch/list state is re-derived per call, not
# cached), so one shared instance per source is safe to reuse across jobs.
_CONNECTOR_REGISTRY: dict[str, Connector] = {
    SlackConnector.source_name: SlackConnector(),
    GitHubConnector.source_name: GitHubConnector(),
    JiraConnector.source_name: JiraConnector(),
    TeamsConnector.source_name: TeamsConnector(),
    AzureDevOpsConnector.source_name: AzureDevOpsConnector(),
    ConfluenceConnector.source_name: ConfluenceConnector(),
    SharePointConnector.source_name: SharePointConnector(),
    RunbooksConnector.source_name: RunbooksConnector(),
    GoogleDriveConnector.source_name: GoogleDriveConnector(),
    GitLabConnector.source_name: GitLabConnector(),
    NotionConnector.source_name: NotionConnector(),
    ServiceNowConnector.source_name: ServiceNowConnector(),
    PagerDutyConnector.source_name: PagerDutyConnector(),
}

# One shared, in-process limiter for every job this worker process runs --
# see `app.shared.rate_limiter`'s module docstring for exactly what this
# does and does not guarantee (per-process, not cross-process/distributed).
_rate_limiter = TokenBucketRateLimiter()

# Worker-owned keys are intentionally namespaced away from connector-specific
# user configuration. The checkpoint is a complete, versioned envelope so a
# future shape change can reject old state safely instead of guessing.
_CHECKPOINT_CONFIG_KEY = "_ingestion_checkpoint"
_CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class _ItemProcessingResult:
    documents_processed: int
    chunks_embedded: int


class IngestionSafetyLimitError(RuntimeError):
    """A connector payload exceeded an operator-configured resource bound."""

# `ProcessedDocument.content_type` -> `retrieval.schemas.CollectionName`
# (PROJECT_PLAN.md section 8.2's collection names; see
# `app.database.models.retrieval_models`'s module docstring for the same
# mapping stated from retrieval's side). Lives here, not in ingestion/schemas
# or retrieval/schemas: it names a `CollectionName` (retrieval-owned) keyed
# by a `ContentType` (ingestion-owned), so it belongs to whichever module
# depends on both -- ingestion depends on retrieval (this file), never the
# reverse (import-linter's "retrieval does not depend on ingestion"
# contract), so retrieval/schemas.py cannot hold this mapping itself.
_CONTENT_TYPE_TO_COLLECTION: dict[ContentType, CollectionName] = {
    "document": "documentation",
    "code": "code",
    "chat": "conversations",
}


def _connector_config_fingerprint(source: str, config: dict[str, Any]) -> str:
    """Fingerprint only operator-supplied connector configuration.

    Worker-owned keys (all underscore-prefixed) change during a sync and must
    not invalidate the very checkpoint they contain. JSONB values are JSON
    serializable; compact canonical encoding makes the hash stable across
    dictionary ordering and process restarts.
    """
    operator_config = {key: value for key, value in config.items() if not key.startswith("_")}
    payload = json.dumps(
        {"source": source, "config": operator_config},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_ingestion_checkpoint(
    *,
    source: str,
    config: dict[str, Any],
    force_full_sync: bool,
    now: datetime,
    ttl_seconds: int,
) -> tuple[datetime | None, str, str | None] | None:
    """Validate and decode a durable page checkpoint, or ignore it safely."""
    raw = config.get(_CHECKPOINT_CONFIG_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        updated_at = datetime.fromisoformat(str(raw["updated_at"]))
        if updated_at.tzinfo is None:
            return None
        age_seconds = (now - updated_at).total_seconds()
        if age_seconds < -300 or age_seconds > ttl_seconds:
            return None
        if raw.get("version") != _CHECKPOINT_VERSION:
            return None
        if raw.get("source") != source or raw.get("force_full_sync") is not force_full_sync:
            return None
        if raw.get("config_fingerprint") != _connector_config_fingerprint(source, config):
            return None
        cursor = raw.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            return None
        since_value = raw.get("since")
        since = datetime.fromisoformat(since_value) if isinstance(since_value, str) else None
        if since is not None and since.tzinfo is None:
            return None
        resume_token = raw.get("resume_token")
        if resume_token is not None and not isinstance(resume_token, str):
            return None
        return since, cursor, resume_token
    except (KeyError, TypeError, ValueError):
        return None


def _build_ingestion_checkpoint(
    *,
    source: str,
    config: dict[str, Any],
    force_full_sync: bool,
    since: datetime | None,
    cursor: str,
    resume_token: str | None,
    now: datetime,
) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_VERSION,
        "source": source,
        "force_full_sync": force_full_sync,
        "since": since.isoformat() if since is not None else None,
        "cursor": cursor,
        "resume_token": resume_token,
        "config_fingerprint": _connector_config_fingerprint(source, config),
        "updated_at": now.isoformat(),
    }


def _validate_fetch_result(
    fetch_result: FetchResult,
    *,
    source: str,
    page_number: int,
    seen_cursors: set[str],
) -> None:
    """Enforce the pagination/resource contract shared by all connectors."""
    settings = get_settings()
    if page_number > settings.ingestion_max_pages_per_attempt:
        raise IngestionSafetyLimitError(
            "Connector exceeded ingestion_max_pages_per_attempt."
        )
    if len(fetch_result.items) > settings.ingestion_max_items_per_page:
        raise IngestionSafetyLimitError(
            "Connector page exceeded ingestion_max_items_per_page."
        )
    if not fetch_result.has_more:
        return
    if not fetch_result.next_cursor:
        raise IngestionSafetyLimitError(
            f"Connector '{source}' returned has_more=True without a next_cursor."
        )
    if fetch_result.next_cursor in seen_cursors:
        raise IngestionSafetyLimitError(
            f"Connector '{source}' repeated a pagination cursor."
        )


async def run_ingestion_job(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    rate_limiter: AsyncRateLimiter | None = None,
    attempt_number: int = 1,
) -> IngestionJob:
    """Run one sync for `connector_config_id` -- incremental if it has a
    `last_synced_at`, full otherwise (PROJECT_PLAN.md section 4.4).
    """
    return await _execute_ingestion_job(
        session,
        connector_config_id,
        force_full_sync=False,
        rate_limiter=rate_limiter,
        attempt_number=attempt_number,
    )


async def reindex(session: AsyncSession, document_id: uuid.UUID) -> IngestionJob:
    """Force a fresh sync of the connector_config that produced
    `document_id`, so its content gets re-fetched and re-processed.

    Not a targeted single-document re-fetch: the `Connector` protocol has no
    "fetch this one item by external_id" method, only `fetch_batch` (a full
    or incremental *sync* over many items) -- see `base.Connector`'s
    docstring. "Reindex this document" is implemented here as "run a full
    resync of whatever connector produced it," which does reprocess the
    target document (along with everything else from that source) -- honest
    about the mechanism actually available today rather than a narrower
    operation this milestone's connector protocol can't perform.

    Milestone 10 RLS note: `documents` is RLS-protected, and this function
    starts from a bare `document_id` with no `Identity`/org context yet --
    the same chicken-and-egg shape `_execute_ingestion_job` has for
    `connector_configs`. Resolved the same way: a narrow, RLS-bypassing
    lookup (`repository.resolve_document_organization_id`) discovers just
    the owning organization_id, `set_tenant_context` is set to it, and only
    then does the real, RLS-scoped `get_document_by_id` query run.
    """
    document_organization_id = await repository.resolve_document_organization_id(session, document_id)
    if document_organization_id is None:
        raise NotFoundError(
            "Document not found.",
            error_code="document.not_found",
            detail={"document_id": str(document_id)},
        )
    await set_tenant_context(session, document_organization_id)

    document = await repository.get_document_by_id(session, document_id)
    if document is None:
        raise NotFoundError(
            "Document not found.",
            error_code="document.not_found",
            detail={"document_id": str(document_id)},
        )

    connector_config = await repository.get_connector_config_for_source(
        session, document.organization_id, document.source
    )
    if connector_config is None:
        raise ConflictError(
            "No connector configuration is registered for this document's source; cannot reindex.",
            error_code="document.reindex_unavailable",
            detail={"source": document.source, "organization_id": str(document.organization_id)},
        )

    return await _execute_ingestion_job(session, connector_config.id, force_full_sync=True)


async def get_job_status(session: AsyncSession, job_id: uuid.UUID) -> IngestionJob:
    """Fetch one ingestion job's current status."""
    row = await repository.get_ingestion_job_by_id(session, job_id)
    if row is None:
        raise NotFoundError(
            "Ingestion job not found.",
            error_code="ingestion_job.not_found",
            detail={"job_id": str(job_id)},
        )
    return IngestionJob.model_validate(row)


async def dead_letter_ingestion_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> IngestionJob:
    """Mark an exhausted attempt for explicit operator replay."""
    await set_tenant_context(session, organization_id)
    row = await repository.update_ingestion_job(
        session,
        job_id,
        status="dead_lettered",
        completed_at=datetime.now(UTC),
    )
    if row is None:
        raise NotFoundError(
            "Ingestion job not found.",
            error_code="ingestion_job.not_found",
            detail={"job_id": str(job_id)},
        )
    await session.commit()
    return IngestionJob.model_validate(row)


# Connector fetches are read-only and therefore safe to retry. Transport
# failures plus the standard transient HTTP statuses are retried; permanent
# 4xx responses still fail immediately. In particular, treating 429/503 as
# permanent used to turn normal provider throttling/outages into whole-job
# failures even when the provider explicitly supplied ``Retry-After``.
_MAX_FETCH_TRANSPORT_RETRIES = 3
_FETCH_RETRY_BACKOFF_CAP_SECONDS = 10.0
_FETCH_RETRY_AFTER_CAP_SECONDS = 60.0
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float | None:
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(delay, _FETCH_RETRY_AFTER_CAP_SECONDS))


async def _fetch_batch_with_retry(
    connector: Connector, client: object, **fetch_kwargs: Any
) -> FetchResult:
    """Call `connector.fetch_batch`, retrying up to
    `_MAX_FETCH_TRANSPORT_RETRIES` times if the connection to the source
    itself failed to establish -- the same "the network is flaky, not the
    code" failure mode already handled for the database side, extended to
    the outbound HTTP calls every connector makes.
    """
    for attempt in range(_MAX_FETCH_TRANSPORT_RETRIES):
        try:
            return await connector.fetch_batch(client, **fetch_kwargs)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code not in _RETRYABLE_HTTP_STATUSES
            ):
                raise
            is_last_attempt = attempt == _MAX_FETCH_TRANSPORT_RETRIES - 1
            if is_last_attempt:
                raise
            retry_after = _retry_after_seconds(exc) if isinstance(exc, httpx.HTTPStatusError) else None
            delay = (
                retry_after
                if retry_after is not None
                else full_jitter_backoff_seconds(attempt, cap=_FETCH_RETRY_BACKOFF_CAP_SECONDS)
            )
            logger.warning(
                "ingestion_fetch_retry",
                attempt=attempt + 1,
                max_attempts=_MAX_FETCH_TRANSPORT_RETRIES,
                delay_seconds=round(delay, 2),
                status_code=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: loop above always returns or raises")


async def _execute_ingestion_job(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    force_full_sync: bool,
    rate_limiter: AsyncRateLimiter | None = None,
    attempt_number: int = 1,
) -> IngestionJob:
    """Shared implementation behind `run_ingestion_job` and `reindex`.

    Fetches pages in a loop, committing every item and then durably saving
    the connector's opaque *next* cursor after the page completes. ARQ
    retries therefore resume at the last completed page rather than
    restarting the remote traversal. `failed_stage` remains the coarse
    operator-facing failure label (fetch/process/persist); the durable cursor
    is the execution-level resume mechanism. If a process dies between the
    item commit and checkpoint commit, one page may be replayed safely: the
    content-hash check makes that replay idempotent.

    Milestone 10 addition (PROJECT_PLAN.md section 12.5): `config_row.
    credential_ref` is the envelope-encrypted blob `core.tenancy.service.
    register_connector` stored, not a usable credential -- decrypted here,
    exactly once per job, into the `ResolvedConnectorConfig` handed to
    `connector.authenticate()`. This is the one place in the whole ingestion
    path a plaintext credential exists at all; it is never persisted,
    logged, or held any longer than this function's own local variables
    live.

    Milestone 10 RLS note: this is the one code path in the whole
    application that cannot call `set_tenant_context` before its first
    query, because it starts from a bare `connector_config_id` with no
    `Identity`/org context yet (a worker job argument, not a request that
    already resolved one) -- and `connector_configs` is itself RLS-protected
    by the row this exact call needs to read. Broken via a narrow,
    RLS-bypassing lookup (`repository.resolve_connector_config_organization_id`,
    see `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`) that answers
    only "which org owns this connector_config," nothing else; only once
    that's known and `set_tenant_context` is set does the real, RLS-scoped
    `get_connector_config` query below run.
    """
    effective_rate_limiter = rate_limiter or _rate_limiter
    organization_id = await repository.resolve_connector_config_organization_id(
        session, connector_config_id
    )
    if organization_id is None:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )
    await set_tenant_context(session, organization_id)

    config_row = await repository.get_connector_config(session, connector_config_id)
    if config_row is None:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    if config_row.status == "disconnected":
        # Fails fast, not slow: a connector the user just deleted
        # (`core.tenancy.service.disconnect_connector`) may still have an
        # `arq` retry already queued from a prior attempt (e.g. one that hit
        # `WorkerSettings.job_timeout`) -- without this check, that retry
        # would re-run the exact same slow sync to the exact same timeout a
        # second and third time before `max_tries` finally gives up. This
        # check turns that into a near-instant no-op instead. Deliberately a
        # `ConflictError`, not a silent early return: a disconnected
        # connector's job history should show *why* it stopped producing
        # documents, not just stop appearing.
        raise ConflictError(
            "This connector has been disconnected; ingestion will not run for it.",
            error_code="connector_config.disconnected",
            detail={"connector_config_id": str(connector_config_id)},
        )

    # Keep plain values for the remainder of this long-running job. A
    # recovery rollback expires SQLAlchemy ORM instances; touching
    # `config_row` afterwards can then trigger an implicit async reload from
    # ordinary attribute access (`MissingGreenlet`) and mask the real error.
    source = config_row.source
    connector_config = dict(config_row.config)
    connector_project_id = config_row.project_id
    last_synced_at = config_row.last_synced_at

    connector = _CONNECTOR_REGISTRY.get(source)
    if connector is None:
        raise ConflictError(
            f"No connector implementation is registered for source '{source}'.",
            error_code="ingestion.unsupported_source",
            detail={"source": source},
        )

    actor = Identity.for_agent("ingestion_worker", organization_id)
    # Lazily resolve an org-wide connector's destination once for the whole
    # job. Doing this inside `_process_one_item` used to add one Neon round
    # trip for every changed document; keeping it lazy also avoids a query
    # for empty incremental syncs.
    ingestion_project_id = connector_project_id
    plaintext_credential = await decrypt_secret(get_kms(), config_row.credential_ref)
    resolved_config = ResolvedConnectorConfig(
        connector_config_id=connector_config_id,
        organization_id=organization_id,
        project_id=connector_project_id,
        source=source,
        credential_ref=plaintext_credential,
        config=connector_config,
    )

    job_row = await repository.insert_ingestion_job(
        session, organization_id=organization_id, connector_config_id=connector_config_id
    )
    # Captured once, right here, while `job_row` is guaranteed freshly
    # loaded -- never re-read via `job_row.id` again below. `session.
    # rollback()` (in the except block further down) unconditionally
    # expires every attribute on every object in the session, `id` included;
    # a later plain `job_row.id` attribute access after that point tries to
    # lazily reload it, which requires a greenlet-spawned async context that
    # a bare attribute access sitting in an ordinary expression (a logging
    # call's kwargs, another call's argument list) does not have --
    # observed in practice as `sqlalchemy.exc.MissingGreenlet` masking
    # whatever the *real* failure was. A plain captured UUID has no such
    # lazy-load behavior at all.
    job_id = job_row.id
    job_row = await repository.update_ingestion_job(
        session,
        job_id,
        status="running",
        started_at=datetime.now(UTC),
        retry_count=max(attempt_number - 1, 0),
    )
    if job_row is None:
        raise RuntimeError("Ingestion job disappeared mid-update.")  # unreachable: just inserted above
    # Committed immediately rather than left pending in the same transaction
    # as everything below: this row must exist and be visible to other
    # sessions (the API's `GET /tenancy/connectors` poll, `get_job_status`)
    # even if the sync itself never gets any further -- see the per-item
    # commit below for why the rest of this function no longer holds one
    # long-lived transaction across the whole sync either.
    await session.commit()
    await set_tenant_context(session, organization_id)  # SET LOCAL does not survive COMMIT

    since = None if force_full_sync else last_synced_at
    cursor: str | None = None
    documents_processed = 0
    pages_fetched = 0
    items_discovered = 0
    items_skipped = 0
    chunks_embedded = 0
    stage = "authenticate"
    client = None
    # Fixed for the whole sync (not threaded through `cursor`, which is
    # purely intra-sync pagination) -- see `FetchResult.resume_token`'s own
    # docstring on why a connector re-decodes this same value on every call
    # rather than the caller updating it mid-sync.
    connector_supports_resume_token = getattr(connector, "supports_resume_token", False)
    resume_token_in = connector_config.get("_resume_token") if connector_supports_resume_token else None
    latest_resume_token: str | None = None
    checkpoint = _load_ingestion_checkpoint(
        source=source,
        config=connector_config,
        force_full_sync=force_full_sync,
        now=datetime.now(UTC),
        ttl_seconds=get_settings().ingestion_checkpoint_ttl_seconds,
    )
    if checkpoint is not None:
        since, cursor, checkpoint_resume_token = checkpoint
        if checkpoint_resume_token is not None:
            resume_token_in = checkpoint_resume_token
            latest_resume_token = checkpoint_resume_token
        logger.info(
            "ingestion_job_resumed_from_checkpoint",
            connector_config_id=str(connector_config_id),
            cursor_fingerprint=hashlib.sha256(cursor.encode()).hexdigest()[:12],
            since=since.isoformat() if since is not None else None,
        )
    seen_cursors: set[str] = {cursor} if cursor is not None else set()
    try:
        client = await connector.authenticate(resolved_config)
        while True:
            stage = "fetch"
            # Two independent budgets, both acquired before every fetch
            # (PROJECT_PLAN.md sections 4.5/10: "per connector, per
            # tenant") -- see `app.ingestion.rate_limiter`'s module
            # docstring for why both are needed and what each is for.
            await effective_rate_limiter.acquire(
                f"connector:{connector_config_id}", connector.requests_per_second
            )
            await effective_rate_limiter.acquire(
                f"org:{organization_id}",
                get_settings().ingestion_org_max_requests_per_second,
            )
            fetch_kwargs: dict[str, Any] = {"since": since, "cursor": cursor}
            if connector_supports_resume_token:
                fetch_kwargs["resume_token"] = resume_token_in
            with tracer.start_as_current_span("ingestion.fetch_page") as span:
                span.set_attribute("ingestion.source", source)
                span.set_attribute("ingestion.page_number", pages_fetched + 1)
                fetch_result = await _fetch_batch_with_retry(
                    connector, client, **fetch_kwargs
                )
            pages_fetched += 1
            items_discovered += len(fetch_result.items)
            _validate_fetch_result(
                fetch_result,
                source=source,
                page_number=pages_fetched,
                seen_cursors=seen_cursors,
            )
            if fetch_result.resume_token is not None:
                latest_resume_token = fetch_result.resume_token

            if fetch_result.items and ingestion_project_id is None:
                default_project = await tenancy_service.get_default_project(
                    session, actor, organization_id
                )
                ingestion_project_id = default_project.id

            stage = "process_item"
            for raw_item in fetch_result.items:
                assert ingestion_project_id is not None
                # Each item is its own savepoint, committed for real as soon
                # as it persists -- NOT one savepoint spanning the entire
                # sync. Previously a dropped connection partway through a
                # sync (observed against Neon as
                # `asyncpg.exceptions.ConnectionDoesNotExistError` after a
                # multi-minute gap between chunk inserts) discarded every
                # document written so far, not just the one in flight, and
                # a retry re-fetched and re-embedded the entire sync from
                # page 1. Committing per item bounds a connection blip's
                # blast radius to "lose the one item in flight," and makes
                # each document's chunks visible to other sessions as soon
                # as that document is done rather than only once the whole
                # (potentially hour-long) sync finishes.
                with tracer.start_as_current_span("ingestion.process_item") as span:
                    span.set_attribute("ingestion.source", source)
                    item_result = await _process_one_item_with_retry(
                        session,
                        organization_id=organization_id,
                        connector=connector,
                        raw_item=raw_item,
                        resolved_config=resolved_config,
                        project_id=ingestion_project_id,
                    )
                    span.set_attribute(
                        "ingestion.document_changed",
                        bool(item_result.documents_processed),
                    )
                    span.set_attribute(
                        "ingestion.chunks_embedded", item_result.chunks_embedded
                    )
                documents_processed += item_result.documents_processed
                chunks_embedded += item_result.chunks_embedded
                if item_result.documents_processed == 0:
                    items_skipped += 1

            if not fetch_result.has_more:
                break
            assert fetch_result.next_cursor is not None  # validated above
            seen_cursors.add(fetch_result.next_cursor)

            # The page is fully durable at this point: every changed item was
            # committed independently. Save the *next* cursor only after that
            # work completes, so a process death can at worst replay one page
            # (idempotently), never skip one. This is the core timeout fix:
            # ARQ may cancel this attempt at its hard ceiling, but the retry
            # resumes remote traversal here instead of page one.
            checkpoint_value = _build_ingestion_checkpoint(
                source=source,
                config=connector_config,
                force_full_sync=force_full_sync,
                since=since,
                cursor=fetch_result.next_cursor,
                resume_token=latest_resume_token,
                now=datetime.now(UTC),
            )
            await repository.update_ingestion_job(
                session,
                job_id,
                documents_processed=documents_processed,
                pages_fetched=pages_fetched,
                items_discovered=items_discovered,
                items_skipped=items_skipped,
                chunks_embedded=chunks_embedded,
            )
            await tenancy_service.checkpoint_connector_sync(
                session,
                actor,
                organization_id,
                connector_config_id,
                config_patch={_CHECKPOINT_CONFIG_KEY: checkpoint_value},
            )
            await session.commit()
            await set_tenant_context(session, organization_id)
            cursor = fetch_result.next_cursor

        completed_at = datetime.now(UTC)
        job_row = await repository.update_ingestion_job(
            session,
            job_id,
            status="succeeded",
            documents_processed=documents_processed,
            pages_fetched=pages_fetched,
            items_discovered=items_discovered,
            items_skipped=items_skipped,
            chunks_embedded=chunks_embedded,
            completed_at=completed_at,
        )
        completion_config_patch: dict[str, Any] = {_CHECKPOINT_CONFIG_KEY: None}
        if latest_resume_token is not None:
            completion_config_patch["_resume_token"] = latest_resume_token
        await tenancy_service.update_connector_sync_status(
            session,
            actor,
            organization_id,
            connector_config_id,
            status="active",
            last_synced_at=completed_at,
            config_patch=completion_config_patch,
        )
        await session.commit()
    except (Exception, asyncio.CancelledError) as exc:
        # Also catches `asyncio.CancelledError` -- a `BaseException`, not an
        # `Exception`, since Python 3.8, so a bare `except Exception` here
        # silently let it through. This is exactly how `arq` enforces
        # `WorkerSettings.job_timeout`: it runs this task under `asyncio.
        # wait_for(task, timeout_s)` and cancels the task on timeout
        # (`arq.worker.Worker._run_job`) -- which throws `CancelledError`
        # into this exact call stack. Previously that skipped this whole
        # except block (and the `finally` below still ran and closed the
        # connector, but nothing recorded the outcome), leaving the job row
        # stuck at `status="running"` and the connector stuck wherever it
        # was (e.g. `"connecting"`) forever -- with every item already
        # fetched this run still committed (chunks and embeddings included,
        # per the per-item commit above), silently orphaned from a job/
        # connector status that never caught up to them. See
        # `app.ingestion.workers.main.WorkerSettings.job_timeout`'s own
        # docstring: this is the same "arq cancels the job mid-page" failure
        # mode that comment already flagged, previously worked around only
        # by raising the timeout ceiling rather than handling the
        # cancellation itself.
        #
        # `exc_info=True`: this is the ONE place that logs the actual root
        # cause (a dropped connection, an item-processing bug, whatever
        # `exc` is) -- everything below tries to *record* the failure, and
        # if that recording itself fails too (see the nested except below),
        # only this line still shows what originally went wrong.
        logger.warning(
            "ingestion_job_failed",
            job_id=str(job_id),
            connector_config_id=str(connector_config_id),
            stage=stage,
            documents_processed=documents_processed,
            error=str(exc),
            exc_info=True,
        )
        completed_at = datetime.now(UTC)
        # Safe even on failure: a site/channel whose walk didn't reach
        # completion this run simply keeps whatever token it already had
        # (never overwritten), which is still valid to resume from next
        # time -- only sites that *did* complete get a fresher one.
        config_patch = (
            {"_resume_token": latest_resume_token} if latest_resume_token is not None else None
        )
        try:
            # `rollback()` first, unconditionally: whatever raised may have
            # left the session mid-transaction -- either the current item's
            # own uncommitted work (an ordinary processing error), or, if
            # the exception was a dropped connection, the session sitting in
            # the "pending rollback" state SQLAlchemy requires clearing
            # before any further query can run on it at all (surfaces
            # otherwise as a second, masking error: "Can't reconnect until
            # invalid transaction is rolled back"). Safe unconditionally now
            # that items commit for real as they finish: the only thing a
            # rollback here can discard is the one item that never got to
            # commit, not any earlier one.
            await session.rollback()
            await set_tenant_context(session, organization_id)
            # `documents_processed` IS accurate here, unlike the old
            # whole-job-savepoint design: every count it includes was
            # already committed above, so reporting it doesn't claim
            # anything the rollback just erased.
            job_row = await repository.update_ingestion_job(
                session,
                job_id,
                status="failed",
                failed_stage=stage,
                documents_processed=documents_processed,
                pages_fetched=pages_fetched,
                items_discovered=items_discovered,
                items_skipped=items_skipped,
                chunks_embedded=chunks_embedded,
                last_error_type=type(exc).__name__,
                completed_at=completed_at,
            )
            await tenancy_service.update_connector_sync_status(
                session,
                actor,
                organization_id,
                connector_config_id,
                status="error",
                config_patch=config_patch,
            )
            await session.commit()
        except Exception:
            # Observed in practice: after a bad enough connection drop, this
            # same session/connection can be too damaged to reuse at all --
            # even the `rollback()` above doesn't recover it, and the
            # `update_ingestion_job` call above raises a second, unrelated-
            # looking error (e.g. a SQLAlchemy "greenlet_spawn has not been
            # called" internal error) instead of writing anything. Without
            # this fallback, the job row is left stuck at status="running"
            # forever, with the real failure logged above but no durable
            # trace of it in `ingestion_jobs` at all. A brand new session
            # (a fresh connection from the pool) sidesteps whatever state
            # the original one is stuck in.
            logger.warning(
                "ingestion_job_failure_record_write_failed",
                job_id=str(job_id),
                connector_config_id=str(connector_config_id),
                exc_info=True,
            )
            # The first write may have flushed the job-row UPDATE before a
            # later statement failed. Release that transaction (and its row
            # lock) before a fresh session attempts the same UPDATE;
            # otherwise every fallback waits on our own lock until its
            # 30-second command timeout expires.
            try:
                await session.rollback()
            except Exception:
                logger.warning(
                    "ingestion_job_failure_session_rollback_failed",
                    job_id=str(job_id),
                    connector_config_id=str(connector_config_id),
                    exc_info=True,
                )
            # A single fallback attempt isn't always enough: observed in
            # practice, a network unstable enough to kill the original
            # session mid-item can still be down when the *first* fresh
            # session is opened too (a flapping connection, not a one-off
            # drop). Retrying the fallback itself, each attempt on its own
            # brand-new connection, gives a badly-flapping network multiple
            # chances to land a fresh connection in a good window rather
            # than giving up on the very first one.
            for attempt in range(_MAX_FAILURE_RECORD_RETRIES):
                is_last_attempt = attempt == _MAX_FAILURE_RECORD_RETRIES - 1
                try:
                    async with session_scope() as fallback_session:
                        await set_tenant_context(fallback_session, organization_id)
                        job_row = await repository.update_ingestion_job(
                            fallback_session,
                            job_id,
                            status="failed",
                            failed_stage=stage,
                            documents_processed=documents_processed,
                            pages_fetched=pages_fetched,
                            items_discovered=items_discovered,
                            items_skipped=items_skipped,
                            chunks_embedded=chunks_embedded,
                            last_error_type=type(exc).__name__,
                            completed_at=completed_at,
                        )
                        await tenancy_service.update_connector_sync_status(
                            fallback_session,
                            actor,
                            organization_id,
                            connector_config_id,
                            status="error",
                            config_patch=config_patch,
                        )
                    break
                except Exception:
                    if is_last_attempt:
                        # Genuinely nothing left to try: every attempt at
                        # recording the failure, on a fresh connection each
                        # time, failed too. Logged (not swallowed) so the
                        # job row's eventual stuck `status="running"` has a
                        # trace explaining why, then re-raised so arq's own
                        # retry (which doesn't depend on this record) still
                        # gets a chance once the network actually recovers.
                        logger.warning(
                            "ingestion_job_failure_record_write_exhausted",
                            job_id=str(job_id),
                            connector_config_id=str(connector_config_id),
                            attempts=_MAX_FAILURE_RECORD_RETRIES,
                            exc_info=True,
                        )
                        raise
                    delay = full_jitter_backoff_seconds(
                        attempt, cap=_FAILURE_RECORD_RETRY_BACKOFF_CAP_SECONDS
                    )
                    logger.warning(
                        "ingestion_job_failure_record_retry",
                        job_id=str(job_id),
                        connector_config_id=str(connector_config_id),
                        attempt=attempt + 1,
                        max_attempts=_MAX_FAILURE_RECORD_RETRIES,
                        delay_seconds=round(delay, 2),
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
        # Deliberately NOT re-raised for an ordinary `Exception`: this whole
        # call runs inside the one session/transaction the caller's
        # `session_scope()` opened, and that helper's contract is "commit on
        # normal return, rollback on any exception that escapes." Re-raising
        # here used to undo this very `status="failed"` write (and the
        # original `insert_ingestion_job` row with it) the instant it
        # reached that boundary -- every failed ingestion job left zero
        # trace in `ingestion_jobs`, despite this except block's own intent.
        # Returning normally instead lets the failure record actually
        # commit; `run_ingestion_job_task` (the caller that decides on arq
        # retries) checks `job.status` for this reason rather than relying
        # on a caught exception.
        #
        # `CancelledError` is different and IS re-raised, once the failure
        # is durably recorded above: swallowing a cancellation is its own
        # bug independent of this job (it can leave `asyncio.wait_for`'s own
        # bookkeeping -- and arq's retry-vs-give-up decision, which inspects
        # the propagated `CancelledError` itself, `arq.worker.Worker.
        # _run_job` -- believing this task is still running when it isn't).
        # This job's own failure/retry semantics don't depend on the
        # re-raise: `run_ingestion_job_task`'s `except Exception` can't see
        # it either (same `BaseException` reasoning as above), so it always
        # propagates the rest of the way to arq, which is exactly what
        # decides whether to retry a cancelled job.
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        if client is not None:
            await connector.close(client)

    if job_row is None:
        raise RuntimeError("Ingestion job disappeared mid-update.")  # unreachable: updated above
    return IngestionJob.model_validate(job_row)


# Bounded, narrow retry -- not a blanket one. `DBAPIError.connection_
# invalidated` is SQLAlchemy's own signal that the *driver* determined the
# underlying connection itself died (asyncpg's `ConnectionDoesNotExistError`,
# a Windows `OSError` mid-socket-read, etc.) rather than the query being
# wrong -- observed repeatedly against both Neon and this project's own
# flaky local network. Any other `DBAPIError` (a real constraint violation,
# a malformed query) would fail identically on every retry, so those still
# propagate on the first attempt.
_MAX_ITEM_CONNECTION_RETRIES = 3
_ITEM_RETRY_BACKOFF_CAP_SECONDS = 10.0

# Separate, slightly more patient budget for the failure-recording fallback
# below (`_execute_ingestion_job`'s except block) -- by the time that code
# runs, the job is already ending one way or another, so it can afford a
# little more total wait than a mid-sync per-item retry before giving up.
_MAX_FAILURE_RECORD_RETRIES = 3
_FAILURE_RECORD_RETRY_BACKOFF_CAP_SECONDS = 15.0


async def _process_one_item_with_retry(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    connector: Connector,
    raw_item: object,
    resolved_config: ResolvedConnectorConfig,
    project_id: uuid.UUID,
) -> _ItemProcessingResult:
    """Run `_process_one_item` in its own savepoint and commit it, retrying
    up to `_MAX_ITEM_CONNECTION_RETRIES` times if the connection drops
    mid-item.

    Previously a single transient connection blip mid-item meant the whole
    arq job attempt died and had to be retried from page 1 of the sync
    (per-item commits bound how much *work* an earlier blip could lose, but
    not whether *this* attempt survived one at all). Retrying here instead
    costs a few seconds for a blip that resolves quickly, rather than a
    full sync restart -- most valuable on the exact kind of unstable
    connection (Neon's pooler, a flaky local network) that keeps causing
    these mid-sync drops in practice.

    Safe to just retry `_process_one_item` wholesale even after a partial
    failure: it starts by checking the item's `content_hash` against what's
    already persisted and returns early (no re-embedding) if unchanged --
    the same idempotency check that already makes a whole-job arq retry
    cheap applies just as well to retrying one item.
    """
    for attempt in range(_MAX_ITEM_CONNECTION_RETRIES):
        try:
            async with session.begin_nested():
                count = await _process_one_item(
                    session,
                    connector=connector,
                    raw_item=raw_item,
                    resolved_config=resolved_config,
                    project_id=project_id,
                )
            if count.documents_processed == 0:
                # This item only performed the content-hash SELECT. Keep
                # the read transaction open until the page checkpoint
                # rather than paying for COMMIT + a new SET LOCAL round
                # trip for every unchanged document. This is especially
                # important for incremental GitHub reconciliation, where
                # most fetched paths are commonly unchanged. Changed items
                # still commit immediately below, preserving the existing
                # crash-safety guarantee for newly embedded content.
                return count
            await session.commit()
            await set_tenant_context(session, organization_id)  # does not survive COMMIT
            return count
        except (DBAPIError, TimeoutError) as exc:
            is_last_attempt = attempt == _MAX_ITEM_CONNECTION_RETRIES - 1
            # asyncpg may expose its command timeout as the builtin
            # `TimeoutError`, rather than wrapping it in a SQLAlchemy
            # `DBAPIError`. Both that transient statement timeout and an
            # explicitly invalidated connection are safe to retry; genuine
            # SQL/constraint errors still fail immediately.
            is_retryable = isinstance(exc, TimeoutError) or exc.connection_invalidated
            if not is_retryable or is_last_attempt:
                raise
            delay = full_jitter_backoff_seconds(attempt, cap=_ITEM_RETRY_BACKOFF_CAP_SECONDS)
            logger.warning(
                "ingestion_item_connection_retry",
                attempt=attempt + 1,
                max_attempts=_MAX_ITEM_CONNECTION_RETRIES,
                delay_seconds=round(delay, 2),
                error=str(exc),
            )
            await session.rollback()
            await asyncio.sleep(delay)
            # SET LOCAL is transaction-scoped and rollback clears it. A
            # retry without restoring this context can either see no rows
            # or violate RLS, depending on the policy being evaluated.
            await set_tenant_context(session, organization_id)
    raise AssertionError("unreachable: loop above always returns or raises")


async def _process_one_item(
    session: AsyncSession,
    *,
    connector: Connector,
    raw_item: object,
    resolved_config: ResolvedConnectorConfig,
    project_id: uuid.UUID,
) -> _ItemProcessingResult:
    """Normalize, process, and persist one raw item.

    Returns 1 if it produced a new document version, 0 if the fetched
    content was unchanged (idempotent no-op, per DATABASE_DESIGN.md's
    `(organization_id, source, external_id, content_hash)` key) -- the
    caller sums these into `documents_processed`. Any exception here
    propagates to `_execute_ingestion_job`'s caller with the coarse
    `"process_item"` stage already recorded -- see that function's
    docstring on why stage-level granularity stops there.
    """
    raw_document = connector.normalize(raw_item)
    settings = get_settings()
    if len(raw_document.content.encode("utf-8")) > settings.ingestion_max_document_bytes:
        raise IngestionSafetyLimitError(
            "Normalized document exceeded ingestion_max_document_bytes."
        )
    processed = process_document(raw_document)
    if len(processed.chunks) > settings.ingestion_max_chunks_per_document:
        raise IngestionSafetyLimitError(
            "Document exceeded ingestion_max_chunks_per_document."
        )

    existing = await repository.get_latest_document(
        session, resolved_config.organization_id, processed.source, processed.external_id
    )
    if existing is not None and existing.content_hash == processed.content_hash:
        return _ItemProcessingResult(0, 0)  # unchanged -- idempotent no-op

    next_version = (existing.version + 1) if existing is not None else 1
    document_row = await repository.insert_document(
        session,
        organization_id=resolved_config.organization_id,
        project_id=project_id,
        source=processed.source,
        external_id=processed.external_id,
        content_hash=processed.content_hash,
        title=processed.title,
        source_url=processed.source_url,
        version=next_version,
    )
    await repository.insert_document_metadata(
        session, document_id=document_row.id, entries=processed.metadata_entries
    )

    # processed.chunks are never persisted by ingestion itself --
    # `<collection>_chunks` tables are retrieval-owned (Chunk's docstring in
    # ingestion/schemas.py). Handed off here, inside the same savepoint as
    # the Document/document_metadata writes above, so a mid-job failure
    # rolls back the chunks along with the row they belong to.
    collection = _CONTENT_TYPE_TO_COLLECTION[processed.content_type]
    # Only the GitHub connector ever sets a "repo" metadata key -- every
    # other connector leaves this `None`, matching `repo_full_name`'s
    # nullable column (see UpsertChunk's docstring).
    repo_full_name = raw_document.metadata.get("repo")
    upsert_chunks = [
        UpsertChunk(
            document_id=document_row.id,
            organization_id=resolved_config.organization_id,
            project_id=project_id,
            collection=collection,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            source_offset_start=chunk.source_offset_start,
            source_offset_end=chunk.source_offset_end,
            acl_permission_code=document_row.acl_permission_code,
            repo_full_name=repo_full_name,
        )
        for chunk in processed.chunks
    ]
    await retrieval_service.upsert(session, upsert_chunks)
    return _ItemProcessingResult(1, len(upsert_chunks))
