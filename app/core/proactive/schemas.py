"""Pydantic contracts for core/proactive.

Owned by: core/proactive. Same "one schemas.py per module" convention as
every other `core/*` submodule.

Two families of type here, and the distinction matters:

  `CandidateFinding`/`CandidateEvidence` -- a detector's OUTPUT, before
  validation, canonicalization, or persistence. Ephemeral; never stored
  as-is.

  `ProactiveFinding`/`FindingEvidenceRef`/`FindingDetail` -- what is
  actually persisted, and what a caller reads back. `FindingDetail`'s
  `supporting_entities` is resolved AND authorization-filtered at read time
  (see `core.proactive.service`) -- it is never a plain copy of the stored
  evidence rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.proactive.contract import EvidenceEntityType, FindingStatus, FindingType


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_type: EvidenceEntityType
    entity_id: uuid.UUID
    role: str


class CandidateFinding(BaseModel):
    """One detector's raw output for one candidate pattern -- not yet
    validated against `core.proactive.contract`, not yet persisted."""

    model_config = ConfigDict(frozen=True)

    finding_type: FindingType
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    #: Stable canonical grouping key -- see `core.proactive.service.
    #: _fingerprint` for exactly how each finding type constructs this.
    fingerprint: str
    #: The detector's own count, computed from the FULL (unfiltered by any
    #: one caller's permissions) source state. `core.proactive.service`'s
    #: read path may recompute a narrower count per caller -- see
    #: `FindingDetail`.
    support_count: int
    title: str
    summary: str
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class ProactiveFinding(BaseModel):
    """A finding as stored -- and as returned by list endpoints, which
    intentionally do NOT include resolved evidence (see `FindingDetail` for
    that, which additionally applies per-caller authorization)."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    finding_type: FindingType
    status: FindingStatus
    title: str
    summary: str
    fingerprint: str
    support_count: int
    detector_name: str
    first_seen_at: datetime
    last_seen_at: datetime
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FindingEvidenceRef(BaseModel):
    """One evidence entity, resolved and labelled -- only ever included in
    a response when the requesting actor is independently authorized to see
    it (`core.proactive.service`'s per-evidence-row visibility check)."""

    model_config = ConfigDict(frozen=True)

    entity_type: EvidenceEntityType
    entity_id: uuid.UUID
    role: str
    label: str | None = None


class FindingDetail(ProactiveFinding):
    """A finding plus its AUTHORIZED, resolved evidence.

    `support_count` here is the value recomputed against only what THIS
    caller may see -- which can be lower than the stored (detection-time)
    value on the base `ProactiveFinding`, and never higher. If recomputing
    drops it below the finding type's `minimum_support`, the finding is not
    returned at all (`NotFoundError`) rather than shown with a misleading
    partial count -- see `core.proactive.service.get_finding`.
    """

    model_config = ConfigDict(frozen=True)

    supporting_entities: list[FindingEvidenceRef] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    """Outcome of running one detector for one organization.

    Counts only -- never the findings/candidates themselves, matching
    `core.graph.schemas.DiscoveryResult`'s identical reasoning: this is an
    operational summary, and the findings it produced are readable through
    the normal, authorized API afterwards.
    """

    model_config = ConfigDict(frozen=True)

    detector_name: str
    finding_type: FindingType
    organization_id: uuid.UUID
    candidate_count: int = 0
    created_count: int = 0
    updated_count: int = 0
    reactivated_count: int = 0
    unchanged_count: int = 0
    deactivated_count: int = 0
    duration_seconds: float = 0.0
    #: Set when this detector raised -- per this priority's failure-
    #: isolation requirement, one detector's exception must not be mistaken
    #: for "no patterns exist" and must not deactivate any existing finding
    #: of ANY type. `None` means the detector ran to completion.
    error: str | None = None
