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
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import service as tenancy_service
from app.ingestion import repository
from app.ingestion.connectors.base import Connector
from app.ingestion.connectors.github import GitHubConnector
from app.ingestion.connectors.slack import SlackConnector
from app.ingestion.processors.pipeline import process_document
from app.ingestion.schemas import ContentType, IngestionJob, ResolvedConnectorConfig
from app.retrieval import service as retrieval_service
from app.retrieval.schemas import CollectionName, UpsertChunk
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

# One connector instance per known source (PROJECT_PLAN.md section 4.2:
# "source_name must match the `source` value used on connector_configs
# rows"). Connectors are stateless between jobs (see each connector's
# module docstring on why fetch/list state is re-derived per call, not
# cached), so one shared instance per source is safe to reuse across jobs.
_CONNECTOR_REGISTRY: dict[str, Connector] = {
    SlackConnector.source_name: SlackConnector(),
    GitHubConnector.source_name: GitHubConnector(),
}

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


async def run_ingestion_job(session: AsyncSession, connector_config_id: uuid.UUID) -> IngestionJob:
    """Run one sync for `connector_config_id` -- incremental if it has a
    `last_synced_at`, full otherwise (PROJECT_PLAN.md section 4.4).
    """
    return await _execute_ingestion_job(session, connector_config_id, force_full_sync=False)


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
    """
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


async def _execute_ingestion_job(
    session: AsyncSession, connector_config_id: uuid.UUID, *, force_full_sync: bool
) -> IngestionJob:
    """Shared implementation behind `run_ingestion_job` and `reindex`.

    Runs to completion within one call (fetches every page for every
    changed/new item, in a loop) rather than being split into
    separately-retriable per-stage arq tasks. PROJECT_PLAN.md section 4.5's
    "a retry resumes from the failed stage, not from scratch" is honored at
    a coarse grain here: `failed_stage` records *which* stage the job died
    in (fetch/normalize/process/persist) for observability, but a retry
    (arq re-invoking this same function) re-runs the whole sync from the
    top, not from that stage's midpoint -- true stage-level resume would
    need each stage to be its own chained, independently-retriable task,
    which is a larger undertaking flagged here rather than silently assumed
    to already exist.
    """
    config_row = await repository.get_connector_config(session, connector_config_id)
    if config_row is None:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    connector = _CONNECTOR_REGISTRY.get(config_row.source)
    if connector is None:
        raise ConflictError(
            f"No connector implementation is registered for source '{config_row.source}'.",
            error_code="ingestion.unsupported_source",
            detail={"source": config_row.source},
        )

    actor = Identity.for_agent("ingestion_worker", config_row.organization_id)
    resolved_config = ResolvedConnectorConfig(
        connector_config_id=config_row.id,
        organization_id=config_row.organization_id,
        project_id=config_row.project_id,
        source=config_row.source,
        credential_ref=config_row.credential_ref,
        config=config_row.config,
    )

    job_row = await repository.insert_ingestion_job(
        session, organization_id=config_row.organization_id, connector_config_id=connector_config_id
    )
    job_row = await repository.update_ingestion_job(
        session, job_row.id, status="running", started_at=datetime.now(timezone.utc)
    )
    if job_row is None:
        raise RuntimeError("Ingestion job disappeared mid-update.")  # unreachable: just inserted above

    since = None if force_full_sync else config_row.last_synced_at
    documents_processed = 0
    stage = "authenticate"
    client = None
    try:
        # The fetch/normalize/process/persist loop runs inside a savepoint
        # (a nested transaction), not the outer transaction directly. This
        # matters because `job_row` was created in the *outer* transaction,
        # which nothing here ever commits (services never commit their own
        # session -- see core.audit.service's docstring on why; the caller's
        # `session_scope()`/`get_db_session` does that). Without the
        # savepoint, a mid-loop failure would have no clean way to roll back
        # just the failed attempt's writes: rolling back the *whole*
        # transaction would also erase `job_row` itself, making the
        # subsequent "mark this job failed" update impossible to apply.
        # `begin_nested()` rolls back only its own block on exception,
        # leaving `job_row` (and the session generally) intact and usable
        # in the `except` clause below.
        async with session.begin_nested():
            client = await connector.authenticate(resolved_config)
            cursor: str | None = None
            while True:
                stage = "fetch"
                fetch_result = await connector.fetch_batch(client, since=since, cursor=cursor)

                stage = "process_item"
                for raw_item in fetch_result.items:
                    documents_processed += await _process_one_item(
                        session,
                        connector=connector,
                        raw_item=raw_item,
                        resolved_config=resolved_config,
                        actor=actor,
                    )

                if not fetch_result.has_more:
                    break
                cursor = fetch_result.next_cursor

        completed_at = datetime.now(timezone.utc)
        job_row = await repository.update_ingestion_job(
            session,
            job_row.id,
            status="succeeded",
            documents_processed=documents_processed,
            completed_at=completed_at,
        )
        await tenancy_service.update_connector_sync_status(
            session,
            actor,
            config_row.organization_id,
            connector_config_id,
            status="active",
            last_synced_at=completed_at,
        )
    except Exception as exc:
        logger.warning(
            "ingestion_job_failed",
            job_id=str(job_row.id),
            connector_config_id=str(connector_config_id),
            stage=stage,
            error=str(exc),
        )
        # `documents_processed` deliberately NOT reported here: the savepoint
        # above rolled back every document/metadata write this attempt made,
        # so the in-memory counter no longer matches what's actually
        # persisted -- reporting it would claim documents were processed
        # that the rollback just erased. The job row keeps whatever count it
        # already had (0, from `insert_ingestion_job`'s default).
        await repository.update_ingestion_job(
            session,
            job_row.id,
            status="failed",
            failed_stage=stage,
            completed_at=datetime.now(timezone.utc),
        )
        await tenancy_service.update_connector_sync_status(
            session, actor, config_row.organization_id, connector_config_id, status="error"
        )
        raise
    finally:
        if client is not None:
            await connector.close(client)

    if job_row is None:
        raise RuntimeError("Ingestion job disappeared mid-update.")  # unreachable: updated above
    return IngestionJob.model_validate(job_row)


async def _process_one_item(
    session: AsyncSession,
    *,
    connector: Connector,
    raw_item: object,
    resolved_config: ResolvedConnectorConfig,
    actor: Identity,
) -> int:
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
    processed = process_document(raw_document)

    existing = await repository.get_latest_document(
        session, resolved_config.organization_id, processed.source, processed.external_id
    )
    if existing is not None and existing.content_hash == processed.content_hash:
        return 0  # unchanged -- idempotent no-op

    if resolved_config.project_id is not None:
        project_id = resolved_config.project_id
    else:
        # An org-wide connector_config has no project_id of its own; fall
        # back to the organization's default project -- the same policy
        # core.incidents.service.create_incident uses when IncidentCreate
        # omits project_id.
        default_project = await tenancy_service.get_default_project(
            session, actor, resolved_config.organization_id
        )
        project_id = default_project.id

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
        )
        for chunk in processed.chunks
    ]
    await retrieval_service.upsert(session, upsert_chunks)
    return 1
