"""Storage monitoring snapshots and the audit-archive ledger (owned by core/storage and core/audit).

`storage_snapshots` and `storage_snapshot_tables` are global (database-level facts, read only by the platform
operator API). `storage_snapshot_tenants` and `audit_archive_batches` are tenant-owned and row-level secured.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class StorageSnapshot(Base):
    __tablename__ = "storage_snapshots"
    __table_args__ = (Index("ix_storage_snapshots_taken_at", "taken_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    #: MEASURED: `pg_database_size(current_database())`.
    database_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The configured limit (never guessed); NULL when none is configured.
    storage_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    percent_of_limit: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    warning: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    warning_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StorageSnapshotTable(Base):
    __tablename__ = "storage_snapshot_tables"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("storage_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)  # MEASURED
    table_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)  # MEASURED
    index_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)  # MEASURED
    row_estimate: Mapped[int] = mapped_column(BigInteger, nullable=False)  # planner ESTIMATE (reltuples)


class StorageSnapshotTenant(Base):
    __tablename__ = "storage_snapshot_tenants"
    __table_args__ = (Index("ix_storage_snapshot_tenants_org", "organization_id", "snapshot_id"),)

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("storage_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)  # exact (counted under RLS)
    document_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)  # exact
    #: ESTIMATE: this organization's share of the measured chunk and document tables, by row count.
    estimated_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    basis: Mapped[str] = mapped_column(Text, nullable=False)


class AuditArchiveBatch(Base):
    """Records that one organization's audit rows for one month were exported and verified. Never deletes anything."""

    __tablename__ = "audit_archive_batches"
    __table_args__ = (
        CheckConstraint("status IN ('exported', 'verified', 'failed')", name="ck_audit_archive_status"),
        UniqueConstraint("organization_id", "period_start", name="uq_audit_archive_org_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
