"""SQLAlchemy model for `agent_memories` -- persistent, permission-aware
agent memory (Priority 4).

Owned by: database/ (definition) + core/memory (write access) -- the same
ownership discipline every other models file in this project follows.

WHY THE EMBEDDING LIVES ON THIS ROW, NOT IN A SEPARATE TABLE
    `embedding` is a `Vector(384)` column on the memory row itself, rather
    than a `agent_memory_embeddings` child table. This is a direct response
    to the bug Priority 3 found and fixed (see `docs/DATA_LIFECYCLE.md` §5):
    a soft-deleted `documents` row left its derived chunk/embedding rows
    behind in separate tables, and those stayed semantically retrievable --
    "deleted" content the Answer Agent could still quote. Co-locating the
    vector with the record makes that failure mode *structurally impossible*
    here: there is no separate row to leave behind, so deleting or
    superseding a memory cannot orphan its embedding. It is the same row.

    This is affordable specifically because memory is low-volume and
    single-chunk by construction (`_MAX_CONTENT_LENGTH` below), unlike
    documents, which are split into many chunks per source and therefore
    genuinely need a child table.

WHY NOT REUSE THE EXISTING CHUNK TABLES
    `documentation_chunks`/`code_chunks`/`conversations_chunks` all require a
    non-null `document_id` (`ON DELETE CASCADE` to `documents`), and they are
    organization-wide knowledge with no concept of a private owner. A memory
    has no document, and a user-scoped memory must be invisible to everyone
    but its owner. Forcing memory into those tables would mean inventing a
    fake parent document *and* mixing user-private rows into the org-wide
    knowledge corpus -- exactly the "memory becomes a second, unrestricted
    knowledge base" outcome to avoid.

TENANT ISOLATION
    Carries `organization_id` directly (not derived via a join), matching the
    `IncidentTimeline`/`Postmortem`/`IngestionJob`/`*_chunks` convention, so
    the Row-Level Security policy added by migration `f1a2b3c4d5e6` can check
    it without reaching into another table. That migration adds this table to
    the same `tenant_isolation` policy set as every other direct-`organization_id`
    table (`c7d4e8f19a2b`).

FOREIGN KEY CHOICES, AND WHY EACH ONE
    `organization_id` RESTRICT -- content-table convention (`documents`,
    `incidents`): an organization's memory must not silently vanish as a side
    effect of some other delete.

    `owner_user_id` CASCADE -- defense in depth. Priority 3 anonymizes the
    `users` row rather than deleting it (three RESTRICT FKs make deletion
    impossible), so in practice this cascade never fires and
    `core.privacy.service` deletes user-scoped memory explicitly. The
    CASCADE exists so that IF a `users` row is ever genuinely removed, its
    private memory cannot survive as an orphan -- never relied upon as the
    only mechanism.

    `project_id` SET NULL -- matches `connector_configs.project_id`. A
    project-scoped memory whose project disappears must not keep a dangling
    reference; `core.memory.repository`'s retrieval requires a non-null
    `project_id` for `scope="project"`, so such a row becomes unretrievable
    (fails closed) rather than leaking to every project.

    `supersedes_memory_id` SET NULL -- the provenance link is useful but not
    load-bearing; losing it must never block deleting the older memory it
    points at.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base

#: Must match `app.retrieval.embedding.EMBEDDING_DIMENSION` and
#: `retrieval_models._EMBEDDING_DIMENSION` -- duplicated rather than
#: imported, the same "cross-module constant, documented not shared"
#: convention those two already use for this exact value.
_EMBEDDING_DIMENSION = 384


class AgentMemory(Base):
    """One curated, reusable piece of remembered information.

    Deliberately NOT a message log. `agent_executions` already records what
    was asked and how the agent performed (and backs `GET /ask/history`);
    this table holds the small number of durable statements worth recalling
    later, each explicitly created rather than harvested from every turn.
    See `docs/AGENT_MEMORY.md` for the memory-vs-history distinction.
    """

    __tablename__ = "agent_memories"
    __table_args__ = (
        # The retrieval query filters by (organization_id, status) and then
        # by scope/owner before ordering by vector distance -- this index
        # serves that leading filter. No ANN index (HNSW/IVFFlat) on
        # `embedding`: per-organization memory counts are small by design
        # (memory is curated, not harvested), so an exact nearest-neighbour
        # scan over an already-tenant-filtered handful of rows is both
        # faster and exactly accurate. Revisit only with real volume
        # evidence -- adding an ANN index here would also make recall
        # approximate, which is a poor trade for a permission-sensitive set.
        Index("ix_agent_memories_org_status", "organization_id", "status"),
        Index("ix_agent_memories_owner_user_id", "owner_user_id"),
        Index("ix_agent_memories_org_project", "organization_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    #: `"user"` | `"project"`. Set to `"user"` together with a non-null
    #: `owner_user_id`, or `"project"` together with a non-null `project_id`
    #: -- enforced in `core.memory.schemas`, not by a database CHECK, matching
    #: this schema's existing convention of keeping status/scope vocabularies
    #: in the Pydantic layer (`documents.status`, `incidents.status`, ...).
    #: `"organization"` is deliberately absent -- see `docs/AGENT_MEMORY.md`.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    #: The owning person, for `scope="user"`. NULL for every other scope.
    #: A user-scoped memory is retrievable ONLY by this user.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    #: The owning project, for `scope="project"`. NULL for every other scope.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    #: Coarse category, e.g. `"preference"` / `"fact"` /
    #: `"investigation_conclusion"`. Vocabulary lives in
    #: `core.memory.schemas.MemoryType`.
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: NOT NULL on purpose. A memory with no embedding would be silently
    #: unretrievable -- present in listings, invisible to every recall query.
    #: Requiring it means embedding failure surfaces at creation time as a
    #: loud error instead of as a memory that mysteriously never surfaces.
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIMENSION), nullable=False)
    #: Provenance. `source_type` is `"explicit"` when a human asked for this
    #: to be remembered, or names the artifact it was derived from
    #: (`"investigation"`, `"incident"`). NULL/absent rather than invented
    #: when there is genuinely no upstream source.
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: `Identity.audit_tag` of whoever created it (`"user:<uuid>"` /
    #: `"agent:<name>"`) -- the same tagged-actor convention
    #: `incident_timeline.actor` and `audit_logs.actor` use, and deliberately
    #: not an FK, for the same reason: an agent has no `users` row.
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    #: `"active"` | `"superseded"` | `"deleted"`. Only `"active"` rows are
    #: ever returned by recall. `"deleted"` is a tombstone kept just long
    #: enough to make repeated deletion idempotent and observable -- the row's
    #: `content` and `embedding` are cleared at that point, so nothing
    #: recallable survives it (see `core.memory.repository.soft_delete`).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    #: Set on the OLD memory when a new one replaces it, pointing forward is
    #: deliberately not modelled: the new row points BACK at what it
    #: replaced, so the chain is discoverable from the current record without
    #: a second write to the old one beyond its status change.
    supersedes_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="SET NULL"), nullable=True
    )
    memory_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    #: Updated opportunistically when a memory is actually injected into an
    #: agent context -- intended to support future relevance/pruning work.
    #: Nothing reads it yet; it is recorded rather than guessed at later,
    #: since the information is unrecoverable after the fact.
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
