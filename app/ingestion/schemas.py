"""Pydantic contracts for ingestion/.

Owned by: ingestion/. Local to this module except where a type is reused
verbatim from shared/schemas/ (`DocumentStatus`, `IngestionJobStatus` --
API_DESIGN.md "Design conventions": defined once, reused everywhere).

Per this module's declared dependency list (PROJECT_PLAN.md section 9.8:
retrieval, database, shared -- core is deliberately absent), nothing here
imports from core/tenancy, even though a connector needs a connector_config's
data to run. `ResolvedConnectorConfig` is ingestion's own local view of a
`connector_configs` row -- populated by ingestion's own repository reading
that table directly via `database/` (a deliberate, user-confirmed choice:
ingestion reads `connector_configs` directly rather than going through
core.tenancy's service layer or requiring a caller to resolve it first; see
repository.py's docstring, written once that file exists, for the full
reasoning and its DATABASE_DESIGN.md-ownership caveat).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.schemas import DocumentStatus, IngestionJobStatus

# Defined here, not in processors/chunking.py, so `ProcessedDocument` below
# can carry it without an import cycle (chunking.py already imports `Chunk`/
# `RawDocument` from this module; schemas.py must not import back from
# chunking.py). `processors.chunking` imports this type from here instead of
# defining it, so there is exactly one definition.
ContentType = Literal["code", "chat", "document"]

# --- Connector-facing types (PROJECT_PLAN.md section 4.2) -------------------


class ResolvedConnectorConfig(BaseModel):
    """The minimal, connector-facing view of one `connector_configs` row.

    Deliberately not a copy of `core.tenancy.schemas.ConnectorConfig` --
    importing that type here would create an undocumented ingestion -> core
    dependency (section 9.8 lists ingestion's dependencies as
    retrieval/database/shared only). `credential_ref` is still a reference
    into the secrets store, not a resolved secret, at this stage: real secret
    resolution depends on `shared/security` (section 12.5), not yet built.
    Until that exists, connector implementations treat `credential_ref` as
    the literal credential value, flagged as a placeholder at each call site
    -- the same kind of explicitly-flagged gap as
    `core.auth.service._resolve_client_secret`.
    """

    model_config = ConfigDict(frozen=True)

    connector_config_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    source: str
    credential_ref: str
    config: dict[str, Any] = Field(default_factory=dict)


class RawDocument(BaseModel):
    """One piece of content as a connector's `normalize()` produces it -- the
    common shape every source (Slack, GitHub, ...) converges on before the
    processing pipeline (cleaning, chunking) ever sees it.

    Per PROJECT_PLAN.md section 4.1, a connector normalizes; it does not
    clean, chunk, embed, dedupe, or otherwise interpret content -- everything
    beyond this shape is the shared processing pipeline's job (task #11).
    """

    source: str
    external_id: str
    content: str
    title: str | None = None
    source_url: str | None = None
    # Free-form, source-specific facts (author, channel, repo, path, ...) --
    # becomes `document_metadata` rows once persisted; EAV-shaped for the
    # same reason that table is (DATABASE_DESIGN.md).
    metadata: dict[str, str] = Field(default_factory=dict)


class FetchResult(BaseModel):
    """One page of a connector's `fetch_batch()` call.

    `items` are raw, source-native items (a Slack message dict, a GitHub
    content-API response, ...) -- NOT yet `RawDocument`s. PROJECT_PLAN.md
    section 4.2 deliberately keeps `fetch_batch` and `normalize` as two
    separate protocol methods (fetch, then normalize each item), so the
    caller (the worker, task #12) is the one that calls
    `connector.normalize(item)` per item in this list; a connector's
    `fetch_batch` itself does no interpretation. Typed `list[Any]` rather
    than a connector-specific type because that native shape genuinely
    differs per source -- this model only carries the pagination envelope
    around it.

    `next_cursor` is opaque to everything except the connector that produced
    it (cursor-based pagination, so a partial fetch can resume without
    re-processing everything already seen). `has_more=False` with
    `next_cursor=None` marks the end of this sync.

    `resume_token` is a second, independent opaque envelope -- only set by
    connectors that declare `Connector.supports_resume_token = True`
    (`SharePointConnector`, `TeamsConnector`) -- carrying state meant to
    survive *across* separate sync runs, not just across pages of one sync
    the way `next_cursor` does. Unlike `next_cursor`, a connector returns
    its *complete* current cross-sync state on every page it emits (e.g.
    `{"site-1": "<deltaLink>", ...}`), not an incremental delta of it, so
    `ingestion.service._execute_ingestion_job` can simply persist whichever
    `FetchResult` happens to be the sync's last one without needing to
    merge anything itself.
    """

    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    resume_token: str | None = None


# --- Ingestion jobs ----------------------------------------------------------


class IngestionJob(BaseModel):
    """One run of a `connector_config`, as returned by `get_job_status`
    (PROJECT_PLAN.md section 9.8).
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    connector_config_id: uuid.UUID
    status: IngestionJobStatus
    failed_stage: str | None
    documents_processed: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


# --- Documents -----------------------------------------------------------------


class DocumentMetadataEntry(BaseModel):
    """One EAV metadata fact about a document -- matches the keys/values
    that arrived on `RawDocument.metadata` before persistence.
    """

    key: str
    value: str


class Document(BaseModel):
    """One ingested, deduplicated unit of content, as returned by the read
    surface (e.g. for `reindex(document_id)` to look up before re-processing).

    `acl_permission_code` is the document-level ACL gate `retrieval.search()`
    enforces (PROJECT_PLAN.md section 5.4, ENGINEERING_DECISIONS.md #007) --
    `None` means no restriction beyond tenant/project scope. Nothing in
    ingestion sets this to a non-null value yet; see #007's flagged gap.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    source: str
    external_id: str
    content_hash: str
    title: str | None
    source_url: str | None
    status: DocumentStatus
    version: int
    acl_permission_code: str | None
    created_at: datetime
    updated_at: datetime


# --- Processing pipeline output (PROJECT_PLAN.md section 4.6) --------------


class Chunk(BaseModel):
    """One retrieval-sized unit produced by the chunking stage.

    NOT persisted by ingestion itself: `<collection>_chunks` tables are
    retrieval-owned (DATABASE_DESIGN.md's ownership convention -- each table
    is listed under the module that owns writes to it). This is a pure
    in-memory/transport shape ingestion produces and hands to
    `retrieval.upsert(chunks)` (Milestone 5, not yet built) -- there is no
    ORM model or repository function for it in ingestion_models.py.

    `source_offset_start`/`source_offset_end` are character offsets into the
    *cleaned* content the chunking stage ran against (PROJECT_PLAN.md
    section 4.6: "preserving source-anchored offsets for citation").
    """

    chunk_index: int
    content: str
    source_offset_start: int
    source_offset_end: int


class ProcessedDocument(BaseModel):
    """The full output of the Document Processing pipeline (cleaning ->
    metadata extraction -> chunking) for one `RawDocument`.

    Everything the worker (task #12) needs to persist a `Document` row and
    its `document_metadata` rows, and to hand `chunks` off to
    `retrieval.upsert(chunks)`. `content_hash` is computed from the
    *cleaned* content -- see
    `app.ingestion.processors.metadata.compute_content_hash`'s docstring for
    why -- so it, not the connector's raw content, is what backs the
    `documents` idempotency key.

    `content_type` is the classification `processors.chunking.
    classify_content_type` already computed to decide how to chunk --
    carried through here so the worker (`ingestion/service.py`) can map it
    to a `retrieval.schemas.CollectionName` when building `UpsertChunk`s,
    instead of re-deriving the classification from the raw document a
    second time.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    external_id: str
    content_hash: str
    title: str | None
    source_url: str | None
    content_type: ContentType
    metadata_entries: list[DocumentMetadataEntry]
    chunks: list[Chunk]
