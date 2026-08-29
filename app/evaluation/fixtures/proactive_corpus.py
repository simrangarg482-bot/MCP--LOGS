"""Mode 1 proactive-findings fixtures: a small, deterministic set of
findings + evidence spanning two organizations and mixed visibility.

This exercises the READ-SIDE of `core.proactive` -- authorization, mixed-
visibility support recomputation, tenant isolation, and lifecycle exclusion
of deleted evidence -- deterministically, without a database or a live
detector run, the same "small fixture standing in for real rows" shape
`app.evaluation.fixtures.graph_corpus` already uses for `core.graph`.

DETECTION ITSELF IS NOT WHAT THIS FIXTURE TESTS
    `core.proactive`'s two detectors (`_detect_recurring_incident_severity`/
    `_detect_incident_multi_document`) and the upsert/reconcile/idempotency
    behavior that runs them are unit-tested directly against real detector
    code in `tests/core/proactive/test_service.py` (including the mandatory
    "run once, run twice, verify convergence" idempotency test) -- there is
    no live incidents/documents/graph-edges table here to detect *from*.
    This fixture instead represents PRE-COMPUTED findings (as if a
    detection run had already produced them) and tests only whether
    `core.proactive.service`'s read-side authorization rules, applied here
    deterministically against fixture data, correctly gate what a given
    identity may see -- the same division of responsibility `graph_corpus.py`
    already establishes between its own adapter tests and
    `tests/core/graph/test_service.py`'s traversal tests.
"""

from __future__ import annotations

from dataclasses import dataclass

ORG_PRIMARY = "eval-org"
ORG_OTHER = "eval-org-secondary"
PROJECT_PLATFORM = "44444444-4444-5444-8444-444444444444"


@dataclass(frozen=True)
class FindingFixture:
    """One pre-computed finding, addressed by `label` (standing in for a
    real fingerprint)."""

    label: str
    organization_id: str = ORG_PRIMARY
    project_id: str | None = PROJECT_PLATFORM
    finding_type: str = "recurring_incident_severity"
    status: str = "active"
    minimum_support: int = 3


@dataclass(frozen=True)
class EvidenceFixture:
    """One evidence row for a `FindingFixture`, addressed by
    `finding_label`. Mirrors `core.proactive.contract.
    SUPPORTING_ROLE_PREFIX`'s convention (`role` starting with
    `"supporting_"` counts toward support) and `_resolve_evidence`'s rules
    for each `entity_type`.
    """

    finding_label: str
    entity_type: str  # "incident" | "document"
    role: str
    #: Only meaningful for `entity_type="document"` -- `"published"` is
    #: always visible; anything else needs `knowledge:review`. Ignored for
    #: `entity_type="incident"`, whose visibility rides entirely on the
    #: finding-level `incident:read` gate.
    document_status: str = "published"
    #: A deleted evidence entity must never count toward support, at any
    #: permission level -- the lifecycle invariant this fixture's
    #: `proactive-deleted-evidence-negative-*` case exercises.
    deleted: bool = False


FINDINGS: list[FindingFixture] = [
    # A clean, fully-visible recurring-severity finding: 3 incidents, no
    # document evidence at all -- visible to anyone with `incident:read`.
    FindingFixture(label="recurring-severity-platform", minimum_support=3),
    # A multi-document finding: one anchor incident, two supporting
    # documents -- one published (always visible), one proposed (needs
    # `knowledge:review`).
    FindingFixture(
        label="multi-document-checkout-incident",
        finding_type="incident_multi_document",
        minimum_support=2,
    ),
    # Same shape, but its one non-anchor document is soft-deleted -- must
    # never be visible, at any permission level (only 1 evidence row would
    # ever count, below the threshold of 2).
    FindingFixture(
        label="multi-document-with-deleted-evidence",
        finding_type="incident_multi_document",
        minimum_support=2,
    ),
    # A different organization entirely -- must never surface for an
    # `eval-org` caller, regardless of permissions.
    FindingFixture(label="other-org-recurring-severity", organization_id=ORG_OTHER),
]

EVIDENCE: list[EvidenceFixture] = [
    EvidenceFixture("recurring-severity-platform", "incident", "supporting_incident"),
    EvidenceFixture("recurring-severity-platform", "incident", "supporting_incident"),
    EvidenceFixture("recurring-severity-platform", "incident", "supporting_incident"),
    EvidenceFixture("multi-document-checkout-incident", "incident", "anchor_incident"),
    EvidenceFixture(
        "multi-document-checkout-incident",
        "document",
        "supporting_document",
        document_status="published",
    ),
    EvidenceFixture(
        "multi-document-checkout-incident",
        "document",
        "supporting_document",
        document_status="proposed",
    ),
    EvidenceFixture("multi-document-with-deleted-evidence", "incident", "anchor_incident"),
    EvidenceFixture(
        "multi-document-with-deleted-evidence",
        "document",
        "supporting_document",
        document_status="published",
    ),
    EvidenceFixture(
        "multi-document-with-deleted-evidence",
        "document",
        "supporting_document",
        document_status="published",
        deleted=True,
    ),
    EvidenceFixture("other-org-recurring-severity", "incident", "supporting_incident"),
    EvidenceFixture("other-org-recurring-severity", "incident", "supporting_incident"),
    EvidenceFixture("other-org-recurring-severity", "incident", "supporting_incident"),
]

FINDINGS_BY_LABEL = {finding.label: finding for finding in FINDINGS}
