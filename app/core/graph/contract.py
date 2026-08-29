"""The relationship contract: which `(source_type, relationship, target_type)`
triples are legal, how each is provenanced, and which direction it runs.

Owned by: core/graph. This module is the single authority on graph vocabulary.
Nothing else may invent an entity type or a relationship type, and an invalid
combination fails loudly rather than being stored and discovered later.

TWO CLASSES OF RELATIONSHIP, AND WHY THE SPLIT MATTERS
    `DERIVED_RELATIONSHIPS` are stored as rows in `knowledge_graph_edges`.
    They exist because nothing in the relational schema records them.

    `FOREIGN_KEY_RELATIONSHIPS` are NEVER stored. They are already enforced
    by Postgres foreign keys, so `core.graph.service` resolves them live from
    the source tables at traversal time. Storing copies could only add
    staleness and a leak path -- the exact Priority 3 failure mode where
    derived rows outlived their source. The least stale-able derived data is
    derived data you never stored.

    Both classes surface through the same traversal API, so a caller sees one
    coherent graph; only the write path differs.

ENTITY TYPES ARE LIMITED TO WHAT ACTUALLY EXISTS
    Notably absent: `service`, `system`, `application`, `component`. The
    repository has NO such entity -- verified across every model file. The
    closest thing is `incidents.owner_team`, a nullable free-text team label
    with no table, no id and no lifecycle. Manufacturing a `service` node
    from that string would be inventing an entity, and every edge touching it
    would be unauthorizable (nothing owns it) and undeletable (nothing
    governs its lifecycle). Deferred honestly -- see
    `docs/KNOWLEDGE_GRAPH.md`.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: Every entity type the graph can reference. Each one is a real, addressable
#: row in an existing table with its own organization scope and lifecycle:
#:   incident       -> `incidents.id`
#:   postmortem     -> `postmortems.id`
#:   document       -> `documents.id`
#:   project        -> `projects.id`
#:   investigation  -> `incident_timeline.id` where `event_type='investigation'`
#:                     (there is no `investigations` table -- an investigation
#:                     result is persisted as a timeline row by
#:                     `core.incidents.service.record_investigation_result`,
#:                     with its evidence/hypotheses in `event_data`)
EntityType = Literal["incident", "postmortem", "document", "project", "investigation"]

#: Every relationship type. Small and concrete; each maps to something the
#: repository can actually demonstrate.
RelationshipType = Literal[
    "documents",        # a document was written about an incident
    "related_to",       # two incidents are related (symmetric)
    "has_postmortem",   # an incident's postmortem
    "belongs_to",       # an entity's owning project
    "investigated_by",  # an incident's investigation timeline entry
]

#: `"foreign_key"`             -- resolved live from a real FK. Certain.
#: `"deterministic_extraction"`-- read from a real stored field (no inference,
#:                                no model). Certain.
#: `"manual"`                  -- asserted by an authorized human. Certain as
#:                                an assertion; the human is the provenance.
#: There is deliberately no `"inferred"`/`"llm"` value: nothing in this
#: implementation infers relationships, and declaring a provenance kind
#: nothing can produce would be a fictional contract.
ProvenanceType = Literal["foreign_key", "deterministic_extraction", "manual"]


class RelationshipSpec(NamedTuple):
    """One legal `(source, relationship, target)` triple and its semantics."""

    source_type: EntityType
    relationship_type: RelationshipType
    target_type: EntityType
    provenance_type: ProvenanceType
    #: True when the relationship means the same thing in both directions, so
    #: traversal must match the starting entity against BOTH endpoint columns
    #: and only ONE canonical row is ever stored. Storing both `A->B` and
    #: `B->A` would double every symmetric fact and require both copies to be
    #: kept in sync forever.
    symmetric: bool
    #: Human-readable meaning, surfaced in `docs/KNOWLEDGE_GRAPH.md` and in
    #: the API response so a caller never has to guess what an edge asserts.
    meaning: str


#: Stored in `knowledge_graph_edges`. Only relationships with no FK to ride on.
DERIVED_RELATIONSHIPS: tuple[RelationshipSpec, ...] = (
    RelationshipSpec(
        source_type="document",
        relationship_type="documents",
        target_type="incident",
        provenance_type="deterministic_extraction",
        symmetric=False,
        meaning=(
            "This document was written about that incident. Extracted from the "
            "`source_incident_id` entry in `document_metadata`, which "
            "`core.knowledge.service.propose_document` writes (reached in "
            "practice via the `propose_runbook_update` MCP tool). The forward "
            "direction was already readable one document at a time; nothing "
            "could ask the reverse question -- 'which runbooks came out of "
            "this incident?' -- before this edge existed."
        ),
    ),
    RelationshipSpec(
        source_type="incident",
        relationship_type="related_to",
        target_type="incident",
        provenance_type="manual",
        symmetric=True,
        meaning=(
            "Two incidents are related (recurrence, shared cause, shared "
            "blast radius). Nothing in the schema records this and it cannot "
            "be derived deterministically, so it is asserted explicitly by a "
            "human holding `incident:write` on BOTH incidents' projects. "
            "Note what this deliberately is NOT: the existing "
            "`search_similar_incidents` tool computes similarity at query "
            "time from vectors, which is a different claim -- 'these read "
            "alike' is not 'these are related', and the graph does not "
            "silently promote one into the other."
        ),
    ),
)

#: NEVER stored -- resolved live from the source tables. See module docstring.
FOREIGN_KEY_RELATIONSHIPS: tuple[RelationshipSpec, ...] = (
    RelationshipSpec(
        source_type="incident",
        relationship_type="has_postmortem",
        target_type="postmortem",
        provenance_type="foreign_key",
        symmetric=False,
        meaning="Resolved live from `postmortems.incident_id` (ON DELETE RESTRICT).",
    ),
    RelationshipSpec(
        source_type="incident",
        relationship_type="belongs_to",
        target_type="project",
        provenance_type="foreign_key",
        symmetric=False,
        meaning="Resolved live from `incidents.project_id` (ON DELETE RESTRICT).",
    ),
    RelationshipSpec(
        source_type="document",
        relationship_type="belongs_to",
        target_type="project",
        provenance_type="foreign_key",
        symmetric=False,
        meaning="Resolved live from `documents.project_id` (ON DELETE RESTRICT).",
    ),
    RelationshipSpec(
        source_type="incident",
        relationship_type="investigated_by",
        target_type="investigation",
        provenance_type="foreign_key",
        symmetric=False,
        meaning=(
            "Resolved live from `incident_timeline.incident_id` (ON DELETE "
            "CASCADE) restricted to `event_type='investigation'` rows -- the "
            "shape `record_investigation_result` actually writes."
        ),
    ),
)

ALL_RELATIONSHIPS: tuple[RelationshipSpec, ...] = (
    DERIVED_RELATIONSHIPS + FOREIGN_KEY_RELATIONSHIPS
)

_DERIVED_BY_TRIPLE = {
    (spec.source_type, spec.relationship_type, spec.target_type): spec
    for spec in DERIVED_RELATIONSHIPS
}
_ALL_BY_TRIPLE = {
    (spec.source_type, spec.relationship_type, spec.target_type): spec
    for spec in ALL_RELATIONSHIPS
}

ENTITY_TYPES: frozenset[str] = frozenset(
    {spec.source_type for spec in ALL_RELATIONSHIPS}
    | {spec.target_type for spec in ALL_RELATIONSHIPS}
)
RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    spec.relationship_type for spec in ALL_RELATIONSHIPS
)


class InvalidRelationshipError(ValueError):
    """A `(source, relationship, target)` triple that the contract forbids.

    A `ValueError` subclass so `core.graph.service` can translate it into the
    project's own `ValidationError` at the boundary, while callers inside this
    package can catch something specific.
    """


def get_spec(
    source_type: str, relationship_type: str, target_type: str
) -> RelationshipSpec:
    """The spec for one triple, or `InvalidRelationshipError`.

    Rejects unknown entity types, unknown relationship types, AND known
    values combined in a way the contract does not allow (e.g.
    `document related_to document`, or `postmortem documents incident`) --
    the last being the case a naive "is the type in the enum?" check would
    wave through.
    """
    if source_type not in ENTITY_TYPES:
        raise InvalidRelationshipError(
            f"unknown source entity type {source_type!r}; "
            f"valid: {sorted(ENTITY_TYPES)}"
        )
    if target_type not in ENTITY_TYPES:
        raise InvalidRelationshipError(
            f"unknown target entity type {target_type!r}; "
            f"valid: {sorted(ENTITY_TYPES)}"
        )
    if relationship_type not in RELATIONSHIP_TYPES:
        raise InvalidRelationshipError(
            f"unknown relationship type {relationship_type!r}; "
            f"valid: {sorted(RELATIONSHIP_TYPES)}"
        )
    spec = _ALL_BY_TRIPLE.get((source_type, relationship_type, target_type))
    if spec is None:
        raise InvalidRelationshipError(
            f"{source_type!r} --{relationship_type}--> {target_type!r} is not a "
            "valid relationship; see app.core.graph.contract for the allowed set"
        )
    return spec


def get_derived_spec(
    source_type: str, relationship_type: str, target_type: str
) -> RelationshipSpec:
    """As `get_spec`, but additionally rejects FK-backed relationships.

    Used by every WRITE path. Attempting to store an edge for a relationship
    Postgres already enforces is a real error, not a harmless duplicate: the
    stored copy would immediately be a second source of truth that can drift
    from the FK and outlive it.
    """
    spec = get_spec(source_type, relationship_type, target_type)
    if (source_type, relationship_type, target_type) not in _DERIVED_BY_TRIPLE:
        raise InvalidRelationshipError(
            f"{source_type!r} --{relationship_type}--> {target_type!r} is backed by a "
            "foreign key and is resolved live at traversal time; it must not be "
            "stored as a graph edge"
        )
    return spec


def canonical_direction(
    spec: RelationshipSpec,
    source_id,
    target_id,
):
    """For a symmetric relationship, the single canonical `(source, target)`
    ordering to store -- lowest UUID first.

    This is what makes symmetric deduplication actually work. Without a
    canonical ordering, `A related_to B` and `B related_to A` are different
    rows under the unique constraint, so the same fact could be stored twice
    and would then need both copies kept in sync forever. Ordering by the
    UUIDs themselves is arbitrary but stable, which is all that is required.

    Directional relationships are returned unchanged -- their direction
    carries meaning.
    """
    if not spec.symmetric:
        return source_id, target_id
    return (source_id, target_id) if str(source_id) <= str(target_id) else (target_id, source_id)
