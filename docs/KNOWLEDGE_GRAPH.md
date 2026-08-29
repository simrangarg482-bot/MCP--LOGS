# Knowledge Graph (`app/core/graph`)

Priority 5. A permission-aware, derived relationship layer over entities
that already exist elsewhere in EKIP.

## What this is not

- **Not a graph database.** No Neo4j, no Cypher, no second persistence
  engine. One PostgreSQL table, `knowledge_graph_edges`
  (`app/database/models/graph_models.py`, migration `a7b8c9d0e1f2`).
- **Not a second knowledge base.** It stores no document content, no
  incident description, no postmortem text -- only `(type, id)` references
  plus a relationship label. Reading an entity's actual content remains the
  job of the service that already owns it (`core.incidents`,
  `core.knowledge`), through that service's own authorization.
- **Not an inference engine.** Nothing here uses an LLM or a similarity
  model to guess a relationship. Every stored edge comes from either a real
  foreign key already enforced by Postgres, or a field that already existed
  and was simply never queried in reverse before, or an explicit human
  assertion.
- **Not a query language.** There is no `POST /graph/query` and no way to
  express an arbitrary traversal. The API surface is three fixed operations
  over a fixed relationship vocabulary (`app/core/graph/contract.py`).

## The core principle: derived, not source of truth

Every relationship the graph can express falls into one of two classes,
and the split is the whole architecture:

| Class | Where it lives | Examples |
|---|---|---|
| `FOREIGN_KEY_RELATIONSHIPS` | **Never stored.** Resolved live from the relational schema at traversal time. | `incident --has_postmortem--> postmortem`, `incident --belongs_to--> project`, `document --belongs_to--> project`, `incident --investigated_by--> investigation` |
| `DERIVED_RELATIONSHIPS` | Stored as rows in `knowledge_graph_edges`. | `document --documents--> incident` (deterministic, read from `document_metadata.source_incident_id`), `incident <--related_to--> incident` (manual, human-asserted) |

Storing a copy of something Postgres already enforces via foreign key would
only add staleness and a leak path -- exactly the failure mode Priority 3
found (soft-deleted `documents` rows whose retrieval chunks outlived them).
The least stale-able derived data is derived data that is never stored.

## Entity types

Five, each a real row in an existing table with its own organization scope
and lifecycle. Notably absent: `service`/`system`/`application`/
`component`. The repository has no such entity -- verified across every
model file. The closest candidates (`incidents.owner_team`, a nullable
free-text label; `document_metadata` EAV keys like `"repo"`) have no table,
no id, and no lifecycle, so treating one as a graph node would mean
inventing an entity that is unauthorizable and undeletable. Deferred
honestly, not built around a fiction.

| Entity type | Backing table | Label shown |
|---|---|---|
| `incident` | `incidents` | `title` |
| `postmortem` | `postmortems` | none (no natural title) |
| `document` | `documents` | `title` |
| `project` | `projects` | `name` |
| `investigation` | `incident_timeline` where `event_type='investigation'` | `"Investigation on <date>"` |

There is no `investigations` table. An investigation result is a timeline
row `core.incidents.service.record_investigation_result` writes, with its
evidence/hypotheses in `event_data` -- see that function's own docstring.

## Relationship types

| Relationship | Direction | Provenance | Meaning |
|---|---|---|---|
| `documents` | `document -> incident` | `deterministic_extraction` | This document was written about that incident. Read from `document_metadata.source_incident_id`, which `core.knowledge.service.propose_document` already writes (reached via the `propose_runbook_update` MCP tool) but which nothing queried in reverse before this. |
| `related_to` | `incident <-> incident` (symmetric) | `manual` | Two incidents are related (recurrence, shared cause, shared blast radius). Cannot be derived deterministically, so it requires an explicit human assertion by someone holding `incident:write` on **both** incidents' projects. Deliberately distinct from `search_similar_incidents`' vector-time similarity -- "these read alike" is not "these are related," and the graph never silently promotes one into the other. |
| `has_postmortem` | `incident -> postmortem` | `foreign_key` | `postmortems.incident_id` (`ON DELETE RESTRICT`). |
| `belongs_to` | `incident/document -> project` | `foreign_key` | `incidents.project_id` / `documents.project_id` (`ON DELETE RESTRICT`). |
| `investigated_by` | `incident -> investigation` | `foreign_key` | `incident_timeline.incident_id` (`ON DELETE CASCADE`), restricted to `event_type='investigation'` rows. |

`core.graph.contract` is the single authority on this table -- nothing else
may invent an entity or relationship type, and an invalid combination
(`contract.get_spec`) fails loudly rather than being silently stored.

There is deliberately no `"inferred"`/`"llm"` provenance value: nothing in
this implementation infers relationships, and declaring a provenance kind
nothing can produce would be a fictional contract.

## Schema

`knowledge_graph_edges` (`app/database/models/graph_models.py`):

- `id`, `organization_id` (FK, `RESTRICT`), `project_id` (FK, `SET NULL`,
  a narrowing hint only -- **never** the authorization boundary).
- `source_entity_type` / `source_entity_id`, `target_entity_type` /
  `target_entity_id` -- **plain UUIDs, no foreign key.** One column cannot
  reference four different tables; this is the same polymorphic tradeoff
  `audit_logs.resource_id` already makes in this schema. The consequence is
  handled explicitly, not ignored: traversal always resolves both endpoints
  live against their own source tables and drops anything that no longer
  resolves, so a stale edge is inert even before cleanup physically removes
  it (see **Lifecycle** below).
- `relationship_type`, `provenance_type`, `provenance_id` (nullable --
  e.g. the `document_metadata` row a `deterministic_extraction` edge was
  read from).
- `status` (`"active"` / `"removed"`), `created_by` (tagged actor string,
  e.g. `"agent:graph_discovery"` or `"user:<uuid>"`).
- `metadata` (JSONB, nullable) -- reserved, unused today; never raw content.
- A unique constraint on `(organization_id, source_entity_type,
  source_entity_id, relationship_type, target_entity_type,
  target_entity_id)` -- deliberately excluding `status`, so a re-discovered
  edge revives its own soft-removed row rather than colliding with it or
  accumulating a duplicate.
- Row-Level Security, added in the creating migration (the same convention
  Priority 4's `agent_memories` migration established): `organization_id =
  current_setting('app.current_organization_id', true)::uuid`.

**Symmetric-relationship deduplication.** `related_to` means the same thing
in both directions, so only one canonical row is ever stored --
`contract.canonical_direction` orders the two ids lexicographically (lowest
`str(uuid)` first) before the write, so asserting the same relationship
from either incident converges on the same row rather than storing `A->B`
and `B->A` as two facts that would need to be kept in sync forever.

## Authorization

**The central invariant: authorization is part of resolution, not a
post-filter.** Every entity the graph can ever return -- the traversal
origin, every node reached during expansion, both endpoints of every
relationship -- passes through `core.graph.service._resolve_entity`, which
re-fetches the row from its own source of truth and re-applies that entity
type's own existing read gate:

- `incident` / `investigation`: `incident:read` on the entity's project
  (`Identity.has_permission`, project-scoped with org-level fallback).
- `document`: visible if `status="published"`; otherwise requires
  `knowledge:review` on the document's project -- the identical rule
  `core.knowledge.service.get_document` already enforces.
- `postmortem`: visible if `status` is `"approved"`/`"published"`;
  otherwise requires `postmortem:write` or `postmortem:approve` on the
  parent incident's project -- the identical rule `core.incidents.service.
  get_postmortem` already enforces.
- `project`: visible to anyone in the same organization (matching
  `core.tenancy.service.list_projects`, which applies no narrower gate).

No new permission code was introduced. Every check above reuses a
permission string and a mechanism (`Identity.has_permission`,
`require_project_permission`) that already existed for that entity type.

**Authorization applies to BOTH ends of every relationship, never just the
traversal origin.** A stored edge is a hint about what to look up, never
trusted on its own to mean "the caller may see this." When traversal
expands from a visible node across an edge, the *other* endpoint is
independently resolved and authorized before it can appear in the result --
an edge naming an entity the caller cannot see is silently dropped, not
surfaced with its details redacted.

**Tenancy** comes entirely from the caller's `Identity.organization_id`.
No function in `core.graph.service` or endpoint in the API router accepts a
client-supplied `organization_id`.

## Traversal

Bounded breadth-first search, `core.graph.service.get_neighborhood`:

- **Depth**: hard-capped at `MAX_TRAVERSAL_DEPTH = 2`
  (`app/core/graph/schemas.py`). A caller-supplied `depth` can only ever
  *narrow* this ceiling -- there is no parameter, in the service or the API,
  through which a caller reaches more hops than the ceiling allows.
- **Cycle protection**: structural. A node is only ever added to the next
  BFS frontier the first time it is reached (`visited`), so a relationship
  that loops back to an already-visited node contributes its edge (if new)
  but never re-expands from that node. `incident <-> incident` `related_to`
  cycles terminate correctly by construction.
- **Result caps**: `DEFAULT_MAX_NODES = 50`, `DEFAULT_MAX_EDGES = 100`,
  enforced *during* expansion (a walk that already hit a cap stops adding to
  the result rather than continuing to do work that gets discarded).
  `GraphNeighborhood.truncated` is set explicitly whenever either cap was
  actually hit -- never left for a caller to infer from counts.
- **Ordering**: deterministic and explainable, never a ranking model.
  Direct relationships sort before deeper ones (BFS depth order); within a
  depth, `foreign_key`/`deterministic_extraction` sort before `manual`, then
  a stable tiebreak on relationship type and entity ids. No ML, no
  PageRank, no learned score.
- **No recursive SQL CTE.** There is no `WITH RECURSIVE` precedent anywhere
  in this codebase (verified: zero hits). Traversal is iterative/
  programmatic, walking one bounded frontier at a time via the same
  repository/service functions every other read in this codebase uses --
  the path with actual precedent here, not a novel SQL construct.

## Lifecycle integration

Two independent barriers, the same discipline `core.memory.repository.
soft_delete`'s own docstring establishes for the Priority 3 lesson:

1. **Query-time exclusion.** `_resolve_entity` re-fetches and re-authorizes
   every endpoint on every read. A soft-deleted document, or an entity that
   no longer exists, simply fails to resolve and is dropped -- this holds
   even if nothing ever physically cleaned up the stale edge row.
2. **Physical cleanup.** `core.graph.service.remove_edges_for_entity`
   deactivates (`status="removed"`) every edge naming a given entity as
   either endpoint. Wired into `core.knowledge.service.reject_document` --
   the one real deletion path that exists for any entity type this graph
   covers today. `core.graph.service.discover_document_incident_edges`'s
   repair half independently deactivates any `documents`-type edge whose
   source document or target incident no longer resolves, so a full
   discovery pass doubles as a self-repair pass.

**A documented, evidence-based gap, not an oversight**: `incidents.
deleted_at` and `postmortems.deleted_at` are declared columns with **zero
reads and zero writes anywhere in the application** (verified by repo-wide
grep) -- there is no deletion path for either entity type in this codebase
today. Consequently there is no hook to wire physical cleanup into for
incident/postmortem deletion; the moment such a path is built, wiring it
into `remove_edges_for_entity` (already generic over entity type) is a
one-line addition, and query-time exclusion already protects correctness
in the meantime, since `_resolve_incident`/`_resolve_postmortem` would
simply need those columns added to their existing filters.

`core.privacy`'s only implemented deletion scope (`"user_data"`)
deliberately never touches documents, incidents, or postmortems -- they are
organization-owned, not user-owned (see `docs/DATA_LIFECYCLE.md`) -- so it
has no graph impact today. The declared-but-unimplemented `"organization"`
scope would need to trigger graph cleanup too, whenever it is built.

## Discovery

`core.graph.service.discover_document_incident_edges` is the one
deterministic discovery pass: it scans `document_metadata` rows keyed
`source_incident_id` (a direct field read, not an inference) and
upserts/repairs the corresponding `documents` edges for one organization.
It is not wired into a scheduler -- no evidence in this codebase justified
adding a new always-running sync system for one relationship type; it is
invokable directly (see `tests/core/graph/test_service.py` for its exact
contract) and can be run manually or from an operational script per
organization, the same shape `core.tenancy.service.list_organizations`'s
own docstring describes for its one existing unscoped, system-level caller.

Rebuildability follows directly: since the graph stores nothing that
isn't re-derivable from source data (foreign keys are never stored at all;
the one derived relationship type is read straight from a real field),
rerunning discovery reconstructs the whole graph from scratch.

## API surface

`app/api/routers/graph.py`, prefix `/knowledge-graph`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/entities/{entity_type}/{entity_id}/relationships` | Depth-1 direct relationships only. |
| `GET` | `/entities/{entity_type}/{entity_id}/related?depth=N` | Bounded traversal. `depth` is clamped `1..MAX_TRAVERSAL_DEPTH` by FastAPI/Pydantic before the service ever sees it. |
| `POST` | `/relationships` | Assert the one manual relationship (`incident related_to incident`). Requires `incident:write` on both incidents' projects. |

No route accepts an `organization_id`. No route accepts an arbitrary
expression or query language.

## Agent / retrieval integration: evaluated, deferred

Priority 5's own spec named the Investigation Agent as a *candidate*
integration point, "to be evaluated, not assumed." It was evaluated
(`app/agents/investigation/evidence.py`) and the graph was **not** wired in
this pass, for a concrete reason rather than a schedule one:

Every source `gather_evidence` collects (code, Slack, postmortems, live
GitHub/Slack) becomes an `EvidenceItem` -- and `EvidenceItem`s are citable:
the Investigation Agent's second sub-stage (hypothesis generation) treats
them as evidence a hypothesis may cite. Feeding graph relationships into
that same channel would be exactly the thing this priority's spec forbids
first: *"graph context is not automatically evidence... no fake `[n]`
citations from edges."* A structural relationship ("this document was
written about that incident") is not evidence for a factual claim the way
a retrieved code diff or Slack thread is.

The correct integration shape is the one Priority 4 already built for
memory: a second, explicitly non-citable channel, separate from
`EvidenceItem`, injected into untrusted context only. Nothing in `app.
agents.investigation` has that channel today -- only `app.agents.answer`
does (`memory_context`, `app.core.memory`'s integration). Building one for
the Investigation Agent is real, scoped agent-prompt work with its own
testing burden, not a one-line addition, and is left for a future priority
with its own evaluation rather than rushed in alongside everything else
here. See `PROJECT_STATUS.md`'s Phase 20 entry for the full reasoning.

## Evaluation harness

Reuses `app.evaluation` entirely -- no second runner. A `"graph"` category
(`app/evaluation/schemas.py`), a `FixtureGraphAdapter`/`RealGraphAdapter`
pair (`app/evaluation/adapters/graph.py`), a small two-organization fixture
graph (`app/evaluation/fixtures/graph_corpus.py`), and a 6-case dataset
(`graph_core_v1.jsonl`) covering: direct relationship recall, multi-hop
traversal with depth-cap enforcement, a permission negative control, a
deleted-entity negative control (checked even at full permission level),
cross-organization isolation, and one deliberately-wrong-expectation
regression control that proves the CI gate actually detects a leak rather
than merely being green. Wired into `scripts/run_evaluation.py`'s default
dataset list -- `uv run python scripts/run_evaluation.py` runs it alongside
every other category with no separate invocation.

## Known limitations

- **No reverse fan-out from `project`.** `belongs_to` (`incident`/
  `document -> project`) is only ever expanded forward. Traversing
  `project -> its incidents` or `project -> its documents` is not
  implemented -- a project can own an unbounded number of either, and doing
  that safely needs a paging design this pass did not build. A genuine,
  documented scope limit, not an oversight.
- **No physical cleanup hook for incident/postmortem deletion**, because no
  deletion path exists for either entity type in this codebase yet (see
  **Lifecycle integration** above).
- **`investigated_by` fan-out is capped at 10** per incident
  (`core.graph.service._MAX_INVESTIGATION_FANOUT`) as a defensive bound, not
  because 10 is a meaningful domain number.
- **No agent integration yet** (see above) -- the graph is reachable today
  only through its own API and the evaluation harness.
