# Proactive Intelligence & Pattern Detection (`app/core/proactive`)

Priority 6. A deterministic, evidence-backed pattern-detection layer over
entities that already exist elsewhere in EKIP.

## What this is not

- **Not an autonomous agent.** No LLM call anywhere in this module. No
  automatic remediation, no automatic incident creation, no automatic
  investigation trigger.
- **Not an anomaly-detection or ML system.** Both detectors are plain
  threshold checks over real, structured columns -- no statistics, no
  forecasting, no learned model.
- **Not a notification system.** There is no email/Slack/webhook-push
  anywhere in this codebase (verified by repository discovery) and this
  priority does not add one. Findings are pull-based, read through
  `GET /insights`, the same way every other EKIP feature is reached today.
- **Not a second graph.** A `ProactiveFinding` is stored in its own table,
  never as a `knowledge_graph_edges` row -- see **Why a separate table**
  below.
- **Not a query language.** Two fixed detectors, a fixed finding-type
  vocabulary, a narrow read-only API (plus one detail endpoint) -- no
  `POST /detect-everything`, no arbitrary trigger surface.

## Repository discovery, and what it ruled out

Before selecting any pattern type, the actual schema was inspected (see
`app/core/proactive/contract.py`'s module docstring for the short version).
The findings that shaped the design:

- **No `service`/`system`/`application`/`component` entity** exists (same
  conclusion Priority 5 already reached, re-verified here). `incidents.
  owner_team` is nullable free text with no canonical identity -- two
  differently-cased/spaced strings meaning the same team would silently
  split into two findings if used as a grouping key. **Rejected** as a
  detector input for exactly this reason, even though it is a real,
  structured-enough column and the source spec allows it as an example.
- **No `investigations` table** and investigation results (`incident_
  timeline` rows with `event_type="investigation"`) carry their
  hypotheses/evidence in free-text-heavy JSONB (`event_data`), not
  structured enough for deterministic grouping. **Rejected**: "repeated
  investigation outcomes" as a pattern type.
- **`search_similar_incidents` is unthresholded, non-deterministic
  embedding search** across every retrieval collection (no dedicated
  "incidents" collection exists), not a stored, deterministic per-incident
  signal. Per this priority's own instruction ("if this cannot be made
  robust with the existing architecture, defer it"), semantic-similarity-
  based pattern detection is **deferred**, not built.
- **`incidents.severity`/`incidents.project_id` are real, canonical,
  already-indexed columns** (`ix_incidents_org_severity`), and the
  Priority 5 graph's stored `document --documents--> incident` relationship
  is a real, deterministic, already-indexed edge
  (`list_active_edges_by_relationship_type`). These two became the two
  implemented finding types.
- **No existing findings/insights/notification subsystem** of any kind
  exists anywhere in `app/` (grepped for `finding`/`insight`/`pattern`/
  `trend`/`recurring`/`cluster`/`proactive`/`notification`/`alert` as real
  identifiers -- only incidental English and the still-mocked monitoring
  evidence *source* stub turned up). A new subsystem was therefore
  justified, not a duplicate of one already there.
- **`core.graph.contract.ProvenanceType` deliberately has no `"inferred"`
  value.** Storing a detected pattern as a graph edge would mean
  fabricating a provenance kind that contract does not support -- see
  `pattern_models.py`'s "Why a separate table" note below.
- **Two existing ARQ worker processes/cron schedules already exist**
  (`app.ingestion.workers` hourly, `app.agents.workers` daily at 02:00,
  the latter already running the closest existing precedent to this
  priority's shape -- the Knowledge Gap Agent's scheduled clustering pass).
  Reused directly; see **Background integration** below.

## Why a separate table, not more graph edges

`core.graph.contract.ProvenanceType` is `"foreign_key"` / `"deterministic_
extraction"` / `"manual"` -- deliberately, with no `"inferred"`/`"pattern"`
value, because nothing in that module infers anything (see that module's
own docstring). A proactive finding IS an inference -- over real,
deterministic, structured signals, never an LLM -- so storing it as a graph
edge would mean stretching a contract that was explicitly designed not to
support that. `proactive_findings`/`proactive_finding_evidence`
(`app/database/models/pattern_models.py`, migration `b1c2d3e4f5a6`) are
their own table pair instead, with their own logical-identity/lifecycle
rules -- see that model file's full rationale.

The graph is still used, correctly: `core.graph.repository.
list_active_edges_by_relationship_type` bounds candidate discovery for the
`incident_multi_document` detector. A graph edge is a hint about which
incidents to look at, never itself the evidence -- the finding's actual
evidence is the resolved `incident`/`document` rows.

## Selected finding types

Two, both defined in `app/core/proactive/contract.py`:

| Finding type | Trigger | Supporting entities | Threshold | Scope | Evidence | Fingerprint |
|---|---|---|---|---|---|---|
| `recurring_incident_severity` | ≥N incidents of severity high/critical created in the same project within a rolling 14-day window | `incidents` (`severity`, `project_id`, `created_at`) | 3 | project | the qualifying `Incident` rows (`role="supporting_incident"`) | `recurring_incident_severity:{project_id}` |
| `incident_multi_document` | ≥N documents linked to the same incident via the graph's stored `documents` relationship | `incidents` + `documents`, bounded via `knowledge_graph_edges` | 2 | project | the anchor `Incident` (`role="anchor_incident"`) + the qualifying `Document` rows (`role="supporting_document"`) | `incident_multi_document:{incident_id}` |

Both thresholds are explicit, initial, placeholder values -- **not**
calibrated against production data (a deferred, documented limitation).

## Architecture

```
Scheduled reconciliation (per organization, every 6 hours)
        v
Bounded candidate discovery (one indexed query per detector, per organization)
        v
Deterministic detector (pure function: source state in, CandidateFinding out)
        v
Contract validation (support >= threshold, evidence roles declared)
        v
Fingerprint (organization_id, finding_type + canonical grouping key)
        v
Upsert (create / update / reactivate / unchanged) + evidence replace
        v
Reconciliation (deactivate previously-active findings this run no longer supports)
        v
Authorized discovery (GET /insights, GET /insights/{id})
```

Detection and persistence are separate: `service._detect_*` functions
return `list[CandidateFinding]` and own no database writes;
`service._run_one_detector` is the only place that calls
`repository.upsert_finding`/`replace_evidence`/`deactivate_finding`.

## Files changed

| Path | New/Modified | Purpose |
|---|---|---|
| `app/database/models/pattern_models.py` | New | `ProactiveFinding`/`ProactiveFindingEvidence` ORM models. |
| `app/database/migrations/versions/b1c2d3e4f5a6_proactive_findings.py` | New | Creates both tables, RLS (direct + join-based policies). |
| `app/core/proactive/contract.py` | New | Finding-type vocabulary, thresholds, evidence-role convention. |
| `app/core/proactive/schemas.py` | New | `CandidateFinding`, `ProactiveFinding`, `FindingDetail`, `ReconciliationResult`. |
| `app/core/proactive/repository.py` | New | Pure data access: upsert/reconcile branching, evidence replace, lifecycle removal. |
| `app/core/proactive/service.py` | New | Two detectors, orchestration, authorized read (mixed-visibility), lifecycle hook. |
| `app/core/incidents/repository.py` | Modified | Added `list_incidents_by_severity_since` (the bounded candidate query). |
| `app/core/knowledge/service.py` | Modified | `reject_document` now also calls `proactive_service.handle_evidence_entity_removed`. |
| `app/agents/workers/tasks.py` | Modified | Added `run_pattern_detection_task`/`scheduled_pattern_detection_scan`. |
| `app/agents/workers/main.py` | Modified | Registered the new task + a 6-hourly cron job on the existing `agents` worker/queue. |
| `app/api/routers/insights.py` | New | `GET /insights`, `GET /insights/{finding_id}`. |
| `app/evaluation/{schemas,runner}.py` | Modified | New `"proactive"` category. |
| `app/evaluation/adapters/proactive.py`, `fixtures/proactive_corpus.py`, `fixtures/proactive_core_v1.jsonl` | New | Fixture adapter + 7-case dataset. |
| `tests/core/proactive/`, `tests/api/test_insights_router.py`, `tests/evaluation/test_proactive_adapter.py` | New | Full test coverage (see below). |

## Authorization and tenant isolation

**Detection is unscoped; resolution is authorized.** `run_detection` (and
both detectors) reads real, current source state directly via repository
functions, with no `Identity` and no permission check anywhere in that
path -- the same "system-level maintenance pass" shape `core.graph.service.
discover_document_incident_edges` already established. It answers "does
this organization's real data support this pattern," not "what can any one
caller see." A finding produced this way is never handed to a caller
directly.

Every caller-reachable read (`list_findings`, `get_finding`) re-resolves
the finding's evidence through `_resolve_evidence`, which re-fetches each
entity from its own source table and re-applies that entity type's
existing read gate (`incident:read`, the document published/`knowledge:
review` rule) -- restating `core.graph.service._resolve_entity`'s exact
invariant for findings.

**Mixed visibility** is handled the same way Priority 5's spec requires:
a finding's `support_count`, as stored, reflects the full (unscoped)
detection-time count. A caller who cannot see every piece of evidence never
sees that number, or the finding's title/summary, or the finding at all if
what they *can* see no longer clears the finding type's own threshold.
`_visible_evidence_and_recomputed_support` is the one function both
`list_findings` and `get_finding` share for this -- support is always
recomputed narrower, per caller, never trusted from the stored row.

**Finding-level scope gate**: `incident:read` on the finding's own
`project_id` (both implemented finding types are single-project-scoped).
A **documented limitation**: this single gate is sufficient only because
neither finding type spans more than one project; a future multi-project
finding type would need a richer scope rule.

**Cross-organization isolation**: structural. `list_findings`/`get_finding`
only ever query rows scoped to `actor.organization_id`; no function
anywhere accepts a client-supplied `organization_id`.

**Deleted evidence** never counts toward support, at any permission level
-- `_resolve_evidence` checks `deleted_at is None` for documents before
authorization is even considered.

## Detection and deduplication

- **Bounded candidate discovery**: one indexed query per detector, per
  organization (`list_incidents_by_severity_since` /
  `list_active_edges_by_relationship_type`) -- never a full scan compared
  against every other entity.
- **Fingerprint**: `finding_type + canonical grouping key` only (a project
  id, or an incident id) -- never a timestamp, a random id, or set-
  iteration order. The unique constraint `uq_proactive_findings_fingerprint`
  (`organization_id`, `fingerprint`) is the DB-level backstop.
- **Repeated detection converges**: `upsert_finding` is queried by logical
  identity before every write; `replace_evidence` deletes-then-reinserts a
  finding's whole evidence set on every upsert (including a no-op "still
  supported, nothing changed" run), so membership drift still converges and
  no evidence table grows without bound. Verified directly:
  `tests/core/proactive/test_service.py::test_run_detection_twice_converges_no_duplicate_findings_or_evidence`
  runs detection twice against identical source state and asserts one
  finding, unchanged evidence count.
- **Reactivation**: a previously-`"inactive"` finding whose fingerprint
  reappears in a later run's candidates is flipped back to `"active"` in
  place, never re-created as a new row.

## Lifecycle

- **When a finding becomes active**: the first detection run whose
  candidates include it (`upsert_finding` returns `"created"`).
- **When support is recomputed**: every scheduled detection run
  (unscoped, against real source state), AND independently, narrower, on
  every authorized read (`list_findings`/`get_finding`, per caller).
- **What happens when support falls below threshold**: at detection time,
  the finding is deactivated (`status="inactive"`, `deactivated_at` set) as
  part of the SAME run's reconciliation step. At read time, the finding is
  simply not returned to that caller (never shown partial).
- **What happens when evidence disappears**: two independent barriers, the
  same discipline Priority 3's own lesson established --
  1. **Query-time**: `_resolve_evidence` re-fetches every evidence entity
     on every read; a gone/deleted/invisible one is dropped and support is
     recomputed narrower.
  2. **Physical cleanup, where a real lifecycle hook exists**: `core.
     knowledge.service.reject_document` calls `proactive_service.
     handle_evidence_entity_removed`, which deletes the stale evidence row
     from every finding that referenced it and recomputes: below threshold
     → deactivated; still supported → support count updated in place.
- **Can a previously-inactive finding reactivate?** Yes -- see above.
- **Honest gap, not invented around**: incidents have **no deletion path
  anywhere in this codebase** (`incidents.deleted_at` is declared but dead
  code -- verified, the same fact Priority 5 already documented for the
  graph). There is therefore no physical-cleanup hook for incident
  evidence; query-time exclusion is what protects reads if/when one is ever
  built, and wiring a hook in later is additive (`handle_evidence_entity_
  removed` is already generic over entity type).

## Background integration

Reuses the **existing** `app.agents.workers` process/queue
(`arq:queue:agents`) -- no new scheduler, no new worker process.
`run_pattern_detection_task`/`scheduled_pattern_detection_scan`
(`app/agents/workers/tasks.py`) are added as a second function/cron job
alongside the Knowledge Gap Agent's existing pair, following that pair's
exact shape: the periodic scan enqueues one per-organization job (so one
organization's pass never blocks another's, and each job gets its own
independent retry/backoff via `full_jitter_backoff_seconds`), and the task
itself opens its own session, calls `set_tenant_context` before any
RLS-protected query, and retries via `Retry(defer=...)` on failure.

**Cadence: every 6 hours** (`00/06/12/18`) -- a deliberate middle point
between ingestion's hourly sync (no freshness reason to match it; nothing
about "3 incidents in 14 days" changes meaningfully hour to hour) and the
knowledge-gap scan's daily cadence (no cost reason to run this rarely;
both detectors are a couple of bounded, indexed SQL queries with zero LLM
calls, unlike knowledge-gap clustering's per-cluster LLM synthesis).

**Synchronous or asynchronous**: asynchronous, matching every other
scheduled EKIP job. Detection never runs inline during ingestion, during a
document's rejection (the one lifecycle hook fires a bounded, indexed
evidence-removal, not a re-detection), or during any request path.

**Retry behavior**: one detector's own exception is caught and isolated
per-finding-type inside `run_detection` -- it never aborts another
detector's run and never touches any existing finding of any type (this
priority's explicit failure-isolation requirement, verified by
`test_one_detector_failure_does_not_affect_the_other_or_existing_findings`).
The task-level `try/except` around the whole per-organization call is a
second, outer safety net (session setup, an unhandled bug) that requests a
whole-organization retry via arq's `Retry` -- safe because detection is
idempotent, never a partial-state risk.

## API surface

`app/api/routers/insights.py`, prefix `/insights`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/insights` | Findings this caller may see, filterable by `status`/`finding_type`. |
| `GET` | `/insights/{finding_id}` | One finding, with authorized, resolved evidence. |

No `POST /insights` (no "detect now" trigger) -- findings are produced
entirely by scheduled/internal detection; this codebase has no precedent
for exposing "run an internal pass" as a public endpoint, and adding one
would be new, unjustified attack surface. No route accepts an
`organization_id`.

## Observability and auditability

Reuses existing infrastructure, builds none new:

- **Structured logging** (`get_logger`), matching `core.graph.service`'s
  `graph_discovery_completed` convention: `proactive_detection_completed`/
  `proactive_detector_failed` carry `detector_name`, `finding_type`,
  `organization_id`, `candidate_count`, `created_count`, `updated_count`,
  `reactivated_count`, `deactivated_count`, `duration_seconds`.
- **Audit trail** (`core.audit.service.record_audit_event`): `proactive.
  finding.created`/`updated`/`reactivated`/`deactivated` events, with
  `finding_type`/`support_count`/`fingerprint` metadata only -- never
  evidence content, matching `core.memory.service.create_memory`'s
  identical reasoning for why its own audit metadata excludes memory text.

No new dashboard endpoint was built -- not required by this priority, and
the structured log fields above already satisfy "reuse existing telemetry,
build no second monitoring system."

## Test and evaluation results

- **839 backend tests passing** (up from 791 after Priority 5; +48 new).
  7/7 import-linter contracts kept. Single Alembic head (`b1c2d3e4f5a6`).
  Migration-coverage guard confirms both tables have a creating migration.
- `tests/core/proactive/`: contract vocabulary, upsert/reconcile branching,
  detector triggers (below/at threshold, stale-edge exclusion), the
  mandatory idempotency test (run detection twice, verify convergence),
  reactivation, failure isolation, cross-tenant isolation, permission
  isolation, mixed-visibility recompute-and-hide (including at full
  permission for deleted evidence), and the lifecycle hook (deactivate/
  update/idempotent-no-op).
- `tests/api/test_insights_router.py`: transport + structural-authorization
  tests (no `organization_id` parameter, exact intended operation surface).
- `tests/evaluation/test_proactive_adapter.py` + `proactive_core_v1.jsonl`
  (7 cases): positive recall, mixed-visibility below-threshold, permission
  raising a finding back above threshold, cross-tenant isolation,
  permission negative control, lifecycle deleted-evidence negative control,
  and one deliberately-wrong-expectation regression-detection control.
  `uv run python scripts/run_evaluation.py` → **51 cases (up from 44), 0
  regressions, VERDICT CLEAN**, exit 0.
- Only local verification was performed -- no claim of a hosted GitHub
  Actions run.

## Remaining limitations (genuine, not a completed-feature list)

- **Semantic/LLM pattern detection**: deferred. `search_similar_incidents`
  is not deterministic/thresholded enough to build on safely today.
- **Threshold calibration on production data**: both thresholds (3, 2) and
  the 14-day window are placeholder initial values.
- **Statistical anomaly detection**: not built; both detectors are plain
  threshold checks.
- **Outbound notifications**: not built; no notification infrastructure
  exists anywhere in this codebase to integrate with.
- **Automatic investigation/remediation creation**: not built.
- **Physical evidence cleanup for incident deletion**: no hook exists,
  because no incident deletion path exists in this codebase yet (query-time
  exclusion covers reads in the meantime).
- **Agent integration through a non-citable context channel**: not built.
  Every existing evidence source the Investigation Agent gathers is
  citable; injecting findings into that channel would violate "a
  relationship/finding is not evidence" (this priority's own explicit
  rule). The correct shape mirrors Priority 4's `memory_context` -- real,
  scoped future work, not attempted here.
- **Only two finding types, both single-project-scoped**: a documented
  scope limit, not an oversight -- see **Selected finding types** above.
- **No pattern ranking / UI / dashboard**: not built; out of scope for
  this priority.
