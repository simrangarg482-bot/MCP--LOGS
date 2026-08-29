"""The finding contract: which finding types are legal, what they trigger
on, and how they are scoped/thresholded/fingerprinted.

Owned by: core/proactive. Single authority on finding vocabulary, the same
role `core.graph.contract` plays for relationship vocabulary. Nothing else
may invent a finding type, and an unsupported one fails loudly rather than
being silently persisted and discovered later.

ONLY TWO FINDING TYPES, BOTH BACKED BY REAL, DETERMINISTIC SIGNALS
    Repository discovery for this priority confirmed there is no `service`/
    `system`/`application`/`component` entity, no `investigations` table
    (investigation results are free-text-heavy `event_data` JSONB, not
    structured enough for deterministic grouping), and no stored incident-
    to-incident similarity (`search_similar_incidents` is unthresholded,
    non-deterministic embedding search across every retrieval collection,
    not a per-incident signal). Both finding types below use only real
    canonical foreign keys and real enum-like columns:

    `recurring_incident_severity` -- `incidents.project_id` (a real FK) +
        `incidents.severity` (a real, small, literal-typed column) +
        `incidents.created_at` (a rolling window). Deliberately does NOT use
        `incidents.owner_team` (nullable free text with no canonical
        identity -- two differently-cased/spaced strings meaning the same
        team would silently split into two findings) as a grouping key,
        even though the source spec allows it as an example; `project_id`
        is the safer, already-canonical choice.

    `incident_multi_document` -- reuses the Priority 5 graph's own stored
        `document --documents--> incident` edges (deterministic_extraction)
        purely to BOUND candidate discovery to incidents that already have
        at least one linked document, never as evidence in itself (a graph
        edge is a hint about what to look up, not a source fact -- see
        `core.graph.service`'s own module docstring). The actual evidence is
        the resolved `document`/`incident` rows.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: Every finding type this implementation can produce. Small and concrete on
#: purpose -- see module docstring for why nothing else qualified today.
FindingType = Literal["recurring_incident_severity", "incident_multi_document"]

#: Every entity type a finding's evidence can reference. Reuses
#: `core.graph.contract.EntityType`'s vocabulary rather than inventing a
#: parallel one, but is its own (smaller) Literal -- not every graph entity
#: type is backing evidence for a finding today, and evidence has its own
#: authorization/resolution rules in `core.proactive.service`, not the
#: graph's.
EvidenceEntityType = Literal["incident", "document"]

#: "active"   -- currently supported at or above threshold as of the last
#:               detection run.
#: "inactive" -- was active, but a later detection run recomputed support
#:               below threshold. Reactivation is allowed: if support rises
#:               again, the same fingerprinted row flips back to "active"
#:               rather than a new row being created (see
#:               `core.proactive.repository.upsert_finding`).
#: Deliberately only two values -- nothing in this implementation
#: represents a human "dismiss" action, so a third "resolved" state would be
#: unearned vocabulary this codebase's own convention (PROJECT_STATUS.md)
#: warns against.
FindingStatus = Literal["active", "inactive"]

#: A finding's minimum support threshold. Explicit per type; a placeholder
#: initial value, not empirically calibrated against production data (that
#: calibration is a deferred, documented limitation -- see
#: `docs/PROACTIVE_INTELLIGENCE.md`).
_RECURRING_SEVERITY_MIN_SUPPORT = 3
_MULTI_DOCUMENT_MIN_SUPPORT = 2

#: How far back `recurring_incident_severity` looks for qualifying
#: incidents. A rolling window, not "all time" -- an incident from a year
#: ago should not keep a finding alive forever; recomputing against a
#: window is what lets a finding naturally go inactive as old incidents age
#: out, without needing a separate expiry mechanism.
RECURRING_SEVERITY_WINDOW_DAYS = 14

#: Only high/critical severities qualify -- a recurrence of `"low"`
#: incidents is not the kind of signal this pattern exists to surface.
RECURRING_SEVERITY_QUALIFYING_VALUES: tuple[str, ...] = ("high", "critical")

#: A role prefixed this way counts toward a finding's `support_count` when
#: it is recomputed (both at detection time and, narrower, at read time
#: against only what one caller may see -- see `core.proactive.service`).
#: Any other role (e.g. `"anchor_incident"`) identifies what the finding is
#: about without itself being one of the repeated occurrences being counted.
SUPPORTING_ROLE_PREFIX = "supporting_"


class FindingSpec(NamedTuple):
    """One legal finding type and its detection/scope/threshold contract."""

    finding_type: FindingType
    #: Every evidence role a candidate of this type may carry, in the exact
    #: form `core.proactive.service`'s detector constructs them -- used to
    #: validate a candidate before it is ever persisted.
    evidence_roles: tuple[str, ...]
    minimum_support: int
    #: Only `"project"` is implemented today -- both finding types are
    #: single-project-scoped by construction. An org-wide finding type is a
    #: legitimate future shape (`ProactiveFinding.project_id` is nullable
    #: for exactly this reason), just not one either detector produces yet.
    scope: Literal["project"]
    meaning: str


FINDING_SPECS: tuple[FindingSpec, ...] = (
    FindingSpec(
        finding_type="recurring_incident_severity",
        evidence_roles=("supporting_incident",),
        minimum_support=_RECURRING_SEVERITY_MIN_SUPPORT,
        scope="project",
        meaning=(
            f"At least {_RECURRING_SEVERITY_MIN_SUPPORT} incidents of severity "
            f"{'/'.join(RECURRING_SEVERITY_QUALIFYING_VALUES)} were created in the same "
            f"project within a rolling {RECURRING_SEVERITY_WINDOW_DAYS}-day window."
        ),
    ),
    FindingSpec(
        finding_type="incident_multi_document",
        evidence_roles=("anchor_incident", "supporting_document"),
        minimum_support=_MULTI_DOCUMENT_MIN_SUPPORT,
        scope="project",
        meaning=(
            f"At least {_MULTI_DOCUMENT_MIN_SUPPORT} documents are linked to the same "
            "incident via the Priority 5 graph's `documents` relationship."
        ),
    ),
)

_SPECS_BY_TYPE: dict[str, FindingSpec] = {spec.finding_type: spec for spec in FINDING_SPECS}
FINDING_TYPES: frozenset[str] = frozenset(_SPECS_BY_TYPE)


class InvalidFindingTypeError(ValueError):
    """An unsupported `finding_type`, raised loudly rather than silently
    stored -- the same discipline `core.graph.contract.
    InvalidRelationshipError` establishes for relationship types."""


def get_finding_spec(finding_type: str) -> FindingSpec:
    spec = _SPECS_BY_TYPE.get(finding_type)
    if spec is None:
        raise InvalidFindingTypeError(
            f"unknown finding type {finding_type!r}; valid: {sorted(FINDING_TYPES)}"
        )
    return spec


def counts_toward_support(role: str) -> bool:
    """Whether an evidence row with this `role` counts toward
    `support_count` when it is recomputed. See `SUPPORTING_ROLE_PREFIX`."""
    return role.startswith(SUPPORTING_ROLE_PREFIX)
