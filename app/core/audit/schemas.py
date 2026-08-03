"""Pydantic contracts for core/audit.

These are local to the audit submodule (per PROJECT_STRUCTURE.md: types
specific to one submodule live in its own schemas.py; only genuinely shared
types live in shared/schemas/). `AuditLogEntry` is the boundary read model --
services convert ORM rows into it so a live SQLAlchemy object never crosses a
module boundary (ARCHITECTURE.md section 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditLogEntry(BaseModel):
    """A single audit-trail record, as returned by the query surface.

    `from_attributes=True` lets the service build this directly from the
    `AuditLog` ORM row (`AuditLogEntry.model_validate(row)`) without a manual
    field-by-field copy. Note `event_metadata` mirrors the ORM attribute name
    (the underlying Postgres column is `metadata`, remapped in the model).

    `organization_id` is nullable, mirroring the ORM column
    (`database/models/core_models.py`'s `AuditLog`): every event core/
    currently records has one (every `Identity` carries a required
    `organization_id`), but the column stays nullable for a possible future
    platform-admin action not scoped to any single company -- an open item,
    not yet designed, flagged on the ORM model itself.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None
    actor: str
    action: str
    resource_type: str
    resource_id: uuid.UUID | None
    event_metadata: dict | None
    occurred_at: datetime


class AuditLogQuery(BaseModel):
    """Filter + pagination for reading the audit trail.

    A dedicated object rather than loose keyword args so the service and the
    future API/MCP layer share one validated contract, and so new filters can
    be added without changing call signatures. All filters are optional and
    AND-combined; omitting them all returns the most recent entries.

    Deliberately does NOT include `organization_id`: matching
    `core.incidents.schemas.IncidentFilter`'s convention, tenant scoping is a
    mandatory, separate argument on `query_audit_log` (enforced via the same
    tenant-isolation guard every other org-scoped read uses), not an optional
    field a caller could simply omit to see every organization's events.
    """

    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    actor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)