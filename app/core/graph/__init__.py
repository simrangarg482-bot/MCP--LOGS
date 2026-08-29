"""core/graph -- a permission-aware derived knowledge graph over existing
EKIP entities (incidents, postmortems, documents, projects, investigations).

This is NOT a graph database. There is no Neo4j, no Cypher, no second
persistence engine: one PostgreSQL table (`knowledge_graph_edges`, see
`app.database.models.graph_models`) stores relationships that have no
foreign key to ride on, and every other relationship is resolved live from
the relational schema at read time (`app.core.graph.contract`). The graph is
derived from data that already exists elsewhere; it is never itself a
source of truth.

Module layout, same convention as every other `core/*` submodule:
    contract.py   -- the entity/relationship vocabulary; the single
                      authority on what triples are legal and how each is
                      provenanced (foreign_key / deterministic_extraction /
                      manual).
    schemas.py    -- Pydantic contracts (`EntityRef`, `GraphRelationship`,
                      `GraphNeighborhood`, ...), plus the hard traversal
                      bounds (`MAX_TRAVERSAL_DEPTH`, `DEFAULT_MAX_NODES`,
                      `DEFAULT_MAX_EDGES`).
    repository.py -- pure data access on `knowledge_graph_edges`.
    service.py    -- authorization, live foreign-key resolution, bounded
                      traversal, manual-relationship creation, deterministic
                      discovery, and lifecycle cleanup.

See `docs/KNOWLEDGE_GRAPH.md` for the full architecture, the authorization
model, and what this module deliberately does not do.
"""
