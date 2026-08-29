"""Mode 1 knowledge-graph fixtures: a small, deterministic graph spanning
two organizations, shaped closely enough after `app.core.graph.contract`'s
real relationship vocabulary to exercise genuine traversal/authorization
behavior without a database.

Deliberately spans MORE than one organization, and includes an unpublished
document, a draft postmortem, and a deleted document -- the minimum shape
that makes permission isolation, tenant isolation, and deleted-entity
exclusion testable at all (the same reasoning `memory_corpus.py`'s own
docstring gives for spanning two owners).

    runbook-pool-tuning ---documents---> incident-pool-exhaustion
    proposed-runbook-draft ---documents--> incident-pool-exhaustion
    rejected-runbook (deleted) ---documents--> incident-pool-exhaustion
    incident-pool-exhaustion ---has_postmortem--> postmortem-pool-exhaustion
    incident-pool-exhaustion ---belongs_to--> platform-project
    incident-pool-exhaustion ---investigated_by--> investigation-pool-exhaustion
    incident-pool-exhaustion <--related_to--> incident-related-timeout
    incident-related-timeout ---has_postmortem--> draft-postmortem-timeout
    other-org-incident  (a different organization entirely, unconnected)
"""

from __future__ import annotations

from dataclasses import dataclass

#: Two organizations, so cross-tenant leakage is genuinely testable.
ORG_PRIMARY = "eval-org"
ORG_OTHER = "eval-org-secondary"

#: The one project most fixtures live in, plus a second used by nothing
#: reachable from `PROJECT_PLATFORM` -- present so a project-scoped
#: permission check has a real "wrong project" to fail against if a future
#: case needs one.
PROJECT_PLATFORM = "22222222-2222-5222-8222-222222222222"
PROJECT_OTHER = "33333333-3333-5333-8333-333333333333"


@dataclass(frozen=True)
class GraphEntityFixture:
    """One pre-existing entity, addressed by `label`.

    `status`/`deleted` mirror exactly the fields `core.graph.service.
    _resolve_entity` reads from the real `documents`/`postmortems` tables --
    a document is `"published"`/`"proposed"`, a postmortem is `"draft"` /
    `"in_review"` / `"approved"` / `"published"`; both are meaningless for
    `"incident"`/`"project"`/`"investigation"` and left `None` there.
    """

    label: str
    entity_type: str
    organization_id: str = ORG_PRIMARY
    project_id: str | None = PROJECT_PLATFORM
    status: str | None = None
    deleted: bool = False


@dataclass(frozen=True)
class GraphEdgeFixture:
    """One relationship. `symmetric` is descriptive only (matching
    `core.graph.contract.RelationshipSpec.symmetric`) -- the fixture
    adapter's traversal treats every edge as reachable from either endpoint
    regardless, the same behavior `core.graph.repository.get_direct_edges`
    has for a real stored edge (see that function's own docstring).
    """

    source_label: str
    relationship_type: str
    target_label: str
    provenance_type: str
    symmetric: bool = False


GRAPH_ENTITIES: list[GraphEntityFixture] = [
    GraphEntityFixture(label="incident-pool-exhaustion", entity_type="incident"),
    GraphEntityFixture(
        label="postmortem-pool-exhaustion", entity_type="postmortem", status="approved"
    ),
    GraphEntityFixture(label="runbook-pool-tuning", entity_type="document", status="published"),
    GraphEntityFixture(
        label="proposed-runbook-draft", entity_type="document", status="proposed"
    ),
    # Rejected: soft-deleted the same way `core.knowledge.service.
    # reject_document` leaves a document -- `status` stays "proposed", only
    # `deleted` flips. Must never be reachable, at ANY permission level.
    GraphEntityFixture(
        label="rejected-runbook", entity_type="document", status="proposed", deleted=True
    ),
    GraphEntityFixture(label="platform-project", entity_type="project"),
    GraphEntityFixture(label="investigation-pool-exhaustion", entity_type="investigation"),
    GraphEntityFixture(label="incident-related-timeout", entity_type="incident"),
    GraphEntityFixture(
        label="draft-postmortem-timeout", entity_type="postmortem", status="draft"
    ),
    # A different organization entirely -- must never surface for a
    # `eval-org` caller, at any depth, regardless of permissions.
    GraphEntityFixture(
        label="other-org-incident",
        entity_type="incident",
        organization_id=ORG_OTHER,
        project_id=PROJECT_OTHER,
    ),
]

GRAPH_EDGES: list[GraphEdgeFixture] = [
    GraphEdgeFixture(
        "runbook-pool-tuning", "documents", "incident-pool-exhaustion", "deterministic_extraction"
    ),
    GraphEdgeFixture(
        "proposed-runbook-draft",
        "documents",
        "incident-pool-exhaustion",
        "deterministic_extraction",
    ),
    GraphEdgeFixture(
        "rejected-runbook", "documents", "incident-pool-exhaustion", "deterministic_extraction"
    ),
    GraphEdgeFixture(
        "incident-pool-exhaustion", "has_postmortem", "postmortem-pool-exhaustion", "foreign_key"
    ),
    GraphEdgeFixture(
        "incident-pool-exhaustion", "belongs_to", "platform-project", "foreign_key"
    ),
    GraphEdgeFixture(
        "incident-pool-exhaustion",
        "investigated_by",
        "investigation-pool-exhaustion",
        "foreign_key",
    ),
    GraphEdgeFixture(
        "incident-pool-exhaustion",
        "related_to",
        "incident-related-timeout",
        "manual",
        symmetric=True,
    ),
    GraphEdgeFixture(
        "incident-related-timeout", "has_postmortem", "draft-postmortem-timeout", "foreign_key"
    ),
]

ENTITIES_BY_LABEL = {entity.label: entity for entity in GRAPH_ENTITIES}
