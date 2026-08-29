"""Pydantic contracts for core/graph.

Owned by: core/graph. Same "one schemas.py per module" convention as every
other `core/*` submodule.

Note what these types deliberately do NOT carry: entity content. An
`EntityRef` is a type, an id, and a short display label resolved from the
source table -- never a document's text, an incident's description, or a
postmortem body. The graph answers "what is connected to what"; reading an
entity's content remains the job of the service that owns it, through its
own authorization.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.graph.contract import EntityType, ProvenanceType, RelationshipType

#: Hard ceiling on traversal depth, not merely a default. Depth 2 answers the
#: real questions ("what came out of this incident, and what is that connected
#: to") while keeping the fan-out explainable and the result set reviewable.
#: Deeper traversal is not exposed at all -- an API-supplied depth cannot
#: exceed this, so no caller can turn one entity into an unbounded walk.
MAX_TRAVERSAL_DEPTH = 2

#: Caps on one traversal's output, enforced during expansion rather than by
#: truncating at the end -- a walk that has already hit the node cap stops
#: expanding instead of continuing to do work whose results get discarded.
DEFAULT_MAX_NODES = 50
DEFAULT_MAX_EDGES = 100


class EntityRef(BaseModel):
    """A reference to an entity, plus the minimum needed to display it.

    `label` is resolved from the source table at traversal time (an
    incident's title, a document's title, a project's name) rather than
    denormalized into the edge row -- so it cannot go stale, and a caller who
    loses visibility of an entity stops seeing its label immediately.
    `label` is `None` when the source row has no natural title.
    """

    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    entity_id: uuid.UUID
    label: str | None = None

    @property
    def key(self) -> tuple[str, uuid.UUID]:
        """Identity for deduplication and cycle detection."""
        return (self.entity_type, self.entity_id)


class GraphRelationship(BaseModel):
    """One relationship, as returned to a caller.

    `provenance_type` is always present and always honest: `"foreign_key"`
    for a relationship resolved live from the relational schema,
    `"deterministic_extraction"` for one read out of a real stored field,
    `"manual"` for one a human asserted. A caller can therefore tell how much
    weight an edge deserves -- which matters because a relationship is NOT
    evidence for a factual claim (see `docs/KNOWLEDGE_GRAPH.md`).
    """

    model_config = ConfigDict(frozen=True)

    source: EntityRef
    relationship_type: RelationshipType
    target: EntityRef
    provenance_type: ProvenanceType
    #: Explains what the relationship asserts, straight from the contract, so
    #: a consumer never has to guess an edge's meaning from its name.
    meaning: str
    #: `None` for live foreign-key relationships, which have no stored row.
    edge_id: uuid.UUID | None = None
    #: How many hops from the traversal's starting entity. 1 for a direct
    #: relationship. Used for deterministic ordering (direct before indirect).
    depth: int = 1


class GraphNeighborhood(BaseModel):
    """The result of a traversal: what was reachable, and what was reached.

    `truncated` is explicit rather than left for a caller to infer from
    counts: silently returning a partial neighborhood that looks complete is
    how a consumer draws a wrong conclusion ("nothing else is connected")
    from a bounded result.
    """

    model_config = ConfigDict(frozen=True)

    origin: EntityRef
    relationships: list[GraphRelationship] = Field(default_factory=list)
    nodes: list[EntityRef] = Field(default_factory=list)
    max_depth_reached: int = 0
    truncated: bool = False


class RelationshipCreate(BaseModel):
    """Request to assert a manual relationship.

    Carries no `organization_id`: tenancy comes from the caller's resolved
    `Identity`, never from the request body -- the same rule
    `core.privacy`/`core.memory` follow.

    Only `related_to` between two incidents is currently assertable this way
    (`contract.DERIVED_RELATIONSHIPS`); every other derived relationship is
    discovered deterministically from source data, and FK-backed ones are
    never stored at all. The service validates the triple, so an
    out-of-contract combination is rejected rather than persisted.
    """

    model_config = ConfigDict(frozen=True)

    source_entity_type: EntityType
    source_entity_id: uuid.UUID
    relationship_type: RelationshipType
    target_entity_type: EntityType
    target_entity_id: uuid.UUID


class DiscoveryResult(BaseModel):
    """Outcome of a deterministic discovery pass.

    Counts only -- never the edges themselves, which could be numerous. A
    discovery pass is an operational action, and its useful output is "how
    much changed", with the edges themselves readable through the normal
    traversal API afterwards.
    """

    model_config = ConfigDict(frozen=True)

    edges_created: int = 0
    edges_revived: int = 0
    edges_unchanged: int = 0
    #: Edges whose source or target no longer resolves to a live, existing
    #: entity, and were therefore removed. Reported separately from creations
    #: because it is the lifecycle half of the pass.
    edges_removed_stale: int = 0
    scanned_at: datetime
