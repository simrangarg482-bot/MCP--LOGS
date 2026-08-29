"""SQLAlchemy models for `proactive_findings`/`proactive_finding_evidence` --
the derived pattern-detection layer (Priority 6).

Owned by: database/ (definition) + core/proactive (write access).

WHAT THIS IS, AND IS NOT
    A `ProactiveFinding` is a derived INTERPRETATION of real source data
    (e.g. "3 high/critical incidents in this project in the last 14 days"),
    never a copy of that data. It carries no incident description, no
    document text -- only a deterministic title/summary generated from
    structured fields, plus references (`ProactiveFindingEvidence`) to the
    real rows that support it. Reading an evidence entity's actual content
    remains the job of the service that already owns it
    (`core.incidents`/`core.knowledge`), through that service's own
    authorization -- exactly the same non-duplication discipline
    `knowledge_graph_edges` (Priority 5) already established.

WHY THIS IS A SEPARATE TABLE FROM `knowledge_graph_edges`, NOT MORE EDGES
    `core.graph.contract.ProvenanceType` is deliberately `"foreign_key"` /
    `"deterministic_extraction"` / `"manual"` -- there is no `"inferred"`/
    `"pattern"` value, and that absence is a documented design decision
    (nothing in that module infers anything). A proactive finding IS an
    inference (over real, deterministic, structured signals -- never an
    LLM), so storing it as a graph edge would mean fabricating a provenance
    kind the graph contract explicitly does not support. Findings and their
    evidence get their own table instead of stretching that contract to fit
    a shape it was not designed for.

NO FOREIGN KEY ON `ProactiveFindingEvidence.entity_id`
    Same polymorphic tradeoff `knowledge_graph_edges.source_entity_id`/
    `audit_logs.resource_id` already make -- one column cannot reference
    both `incidents` and `documents`. The consequence is handled the same
    way: `core.proactive.service` always re-resolves an evidence row's
    entity against its own source table (and re-applies that entity type's
    existing authorization rule) before it can appear in a response, never
    trusting an evidence row alone.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base


class ProactiveFinding(Base):
    """One derived, evidence-backed pattern (Priority 6)."""

    __tablename__ = "proactive_findings"
    __table_args__ = (
        # The stable logical identity of a finding: the same underlying
        # pattern, in the same canonical scope, is always the same row.
        # Repeated detection therefore converges rather than accumulating
        # near-duplicates -- the same reasoning
        # `uq_knowledge_graph_edges_logical_identity` documents for edges.
        UniqueConstraint(
            "organization_id", "fingerprint", name="uq_proactive_findings_fingerprint"
        ),
        Index("ix_proactive_findings_org_status", "organization_id", "status"),
        Index("ix_proactive_findings_org_type", "organization_id", "finding_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # A narrowing scope, not the authorization boundary by itself -- the
    # same role `knowledge_graph_edges.project_id` plays. Every finding type
    # implemented so far always sets this (both are single-project-scoped
    # patterns); nullable because a future org-wide pattern type is a
    # legitimate shape this schema should not need to change to support.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    # "active" | "inactive" -- see `core.proactive.contract.FindingStatus`.
    # Deliberately only two values: nothing in this pass represents a human
    # "dismiss" action, so a third "resolved" state would be unearned
    # vocabulary (PROJECT_STATUS.md's own "do not create unnecessary state
    # machines" rule, restated in this priority's own spec section 17).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    # Deterministic, generated from structured fields -- never an LLM call,
    # never raw source content (a document body, an incident description).
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable canonical grouping key -- see `core.proactive.contract` for how
    # each finding type constructs it. Never a timestamp, a random id, or
    # anything else that would defeat convergence on repeated detection.
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # Recomputed on every detection run against CURRENT source state --
    # never merely incremented. A caller-facing read may recompute this
    # again, narrower, against only evidence that caller can see (see
    # `core.proactive.service`'s "mixed visibility" handling).
    support_count: Mapped[int] = mapped_column(nullable=False)
    # Tagged actor string, e.g. "agent:pattern_detection_agent" -- same
    # convention as `incident_timeline.actor`/`knowledge_graph_edges.
    # created_by`; identifies which detector produced/last touched this row.
    detector_name: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    evidence: Mapped[list["ProactiveFindingEvidence"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class ProactiveFindingEvidence(Base):
    """One reference from a finding to the real entity that supports it.

    No `organization_id` column of its own -- meaningless without its
    parent finding, and never queried except by `finding_id`. Same shape
    (and same RLS treatment: scoped via a join, not a direct column) as
    `document_metadata`. Deleting a finding cascades to its evidence
    (`ondelete="CASCADE"` below): unlike a graph edge, an evidence row has
    no independent meaning once its finding is gone.
    """

    __tablename__ = "proactive_finding_evidence"
    __table_args__ = (
        # Repeated detection must not accumulate duplicate evidence rows for
        # the same (finding, entity, role) -- this is the DB-level half of
        # this priority's idempotency requirement; `core.proactive.
        # repository.replace_evidence` is the application-level half.
        UniqueConstraint(
            "finding_id",
            "entity_type",
            "entity_id",
            "role",
            name="uq_proactive_finding_evidence_identity",
        ),
        Index("ix_proactive_finding_evidence_finding_id", "finding_id"),
        Index("ix_proactive_finding_evidence_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proactive_findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # e.g. "supporting_incident", "anchor_incident", "supporting_document".
    # Convention (see `core.proactive.service`): a role prefixed
    # "supporting_" counts toward `support_count`'s recomputation; any other
    # role (e.g. "anchor_...") identifies the entity the finding is about
    # without itself being one of the repeated occurrences being counted.
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    finding: Mapped[ProactiveFinding] = relationship(back_populates="evidence")
