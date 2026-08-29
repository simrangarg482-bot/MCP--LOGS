# Investigation Agent Reflection & Critique (`app/agents/investigation/critique.py`)

Priority 7. A bounded, evidence-aware critique/reflection stage over the
Investigation Agent's generated root-cause hypotheses.

## What this is not

- **Not an autonomous loop.** There is no `while` and no recursive call
  anywhere in `review_investigation`'s control flow -- reading the function
  top to bottom is reading its complete worst-case call sequence. See
  **Bounded execution** below.
- **Not a truth engine.** The critique interprets whether a hypothesis's
  claim is supported by the CONTENT of the evidence it cites; it never
  independently verifies facts against the outside world, and it never
  manufactures evidence of its own.
- **Not a contradiction engine.** There is no structural mechanism that
  compares evidence pairwise for conflict. The only contradiction signal
  possible is what the semantic critique itself observes directly in the
  evidence text it was given, reported with real evidence-id references --
  see **Contradiction handling** below.
- **Not a second evidence-gathering pass.** Critique fetches nothing new.
  It sees exactly the `evidence`/`hypotheses` already produced by the
  existing Investigation Agent pipeline, nothing more.
- **Not a chain-of-thought leak.** Nothing resembling raw model reasoning
  is ever persisted -- see **What gets persisted** below.

## Repository discovery

Before writing any code, the actual Investigation Agent architecture was
inspected (not assumed from older docs):

- **The investigation graph is one LangGraph node**, `investigation_agent`
  (`app.agents.graph.build_investigation_graph`), whose Python closure
  (`agents.investigation.node.make_investigation_agent_node`) already
  orchestrates multiple internal steps sequentially: gather evidence ->
  generate hypotheses -> (now) critique -> attach to timeline. There was no
  existing precedent in this codebase for "one graph node per logical
  step" for this agent -- the opposite precedent (multi-step orchestration
  inside one node's closure) already existed and is what critique extends.
- **Hypotheses are represented** by `shared.schemas.agent_contracts.
  RootCauseHypothesis` (`description`, `confidence`, `supporting_evidence_ids`)
  -- frozen, no `id` field (indices into the list are how this module
  refers to one).
- **`investigation.hypothesis._validate_hypotheses` already strips
  fabricated evidence references** before a hypothesis is ever constructed
  -- every hypothesis reaching this module's input already has 100% real
  citations. This directly shaped which structural checks were worth
  adding (see **Critique dimensions** below) -- a "does this citation
  exist" check would have been redundant.
- **Investigation results are persisted as `incident_timeline` rows**
  (`event_type="investigation"`, JSONB `event_data`) via `core.incidents.
  service.record_investigation_result` -- confirmed there is no separate
  `investigations`/`hypotheses` table anywhere. Priority 7 adds four new
  keys to that same JSONB dict; no schema/migration change was needed.
- **No grounding node exists in the investigation graph at all.** The
  Answer Agent has one (`agents.answer.grounding.verify_grounding`,
  embedding-similarity-first with an LLM escalation for ambiguous cases) --
  a directly reusable template for critique's "is the claim actually
  supported by the evidence's content" check.
- **`GraphState.confidence_score` is never computed for investigations**
  (only the `answer_question` path runs Confidence Evaluation).
  `RootCauseHypothesis.confidence` is a second, distinct, LLM-self-reported
  number, never independently checked before this priority.
- **LLM calls follow one established convention**: a JSON-object prompt
  parsed by hand (`json.loads` + manual field validation), not `.
  with_structured_output()` -- `investigation.hypothesis.generate_hypotheses`
  is the concrete precedent. `critique.py` follows the identical convention.
- **`agents.retry.call_with_retry`** is the standard retry wrapper (2
  retries, full-jitter backoff, re-raises on exhaustion) -- reused
  unchanged for both the critique call and the revision call.
- **Cost accounting is per-execution, automatic.** `app.agents.telemetry`
  attaches one `UsageMetadataCallbackHandler` to the whole graph run;
  every LLM call any node makes (including critique's) is captured in that
  same aggregate with zero additional wiring. `app.agents.cost_budget.
  check_cost_budget` is a pre-flight, once-per-graph-run gate, not
  per-node -- critique participates in the existing budget automatically
  by virtue of running inside the same graph execution.
- **No per-node OpenTelemetry tracing exists anywhere in `app/agents/`**
  (confirmed by grep) -- only structured `structlog` logging at node/stage
  granularity. Critique follows that same, only existing, observability
  pattern; no new tracing infrastructure was introduced.
- **The evaluation harness's `investigation` category already existed**
  (`app.evaluation.metrics.investigation.find_unsupported_hypotheses`/
  `match_expected_hypotheses`) with its own deterministic, reference-set-
  based "unsupported hypothesis" check -- structurally similar in spirit to
  what critique adds, but purely an evaluation-time metric, never wired
  into the live graph before this priority.

## Why critique lives inside the existing node, not new graph nodes

`agents.graph.build_investigation_graph` compiles to one entry node with
one edge to `END`. Repository discovery found no cycle/loop construct at
the graph level anywhere in this codebase's LangGraph usage, and the
existing precedent for a bounded, multi-step sequence in this exact agent
is Python control flow inside one node's closure, not LangGraph conditional
edges. `review_investigation` is called from inside that same closure
(`agents.investigation.node`) as additional bounded steps, keeping the
compiled graph structurally identical to before this priority -- there is
no cycle construct at the graph level for a bug to ever turn into an
infinite loop, because none exists at all. This also avoids threading
ephemeral critique state (verdict, revision count, structural issues)
through `GraphState` and conditional-edge routing functions for branching
logic nothing outside this one node needs to observe.

## Critique dimensions

Two deterministic, structural dimensions (no LLM call) and two semantic
dimensions (one bounded LLM call), chosen because they are what the
existing evidence/hypothesis representation can actually support -- not a
generic list applied blindly:

| Dimension | Input | Detection method | Possible outcome |
|---|---|---|---|
| `insufficient_information` | total gathered `evidence` count | deterministic: `len(evidence) < investigation_critique_min_evidence_count` | forces `reject`, no LLM call spent |
| `overconfidence:hypothesis_<i>` | one hypothesis's `confidence` + `len(supporting_evidence_ids)` | deterministic: `confidence >= threshold` AND citation count below a configured minimum | fixed confidence penalty applied on acceptance |
| `unsupported_claim:hypothesis_<i>` | one hypothesis's `description` + the CONTENT of its cited evidence | semantic (LLM): does the evidence's text actually support the claim, not merely does the citation exist | fixed confidence penalty applied, or the hypothesis is deemed unsupported |
| `contradictory_evidence` | evidence text pairs the critique itself observes conflicting | semantic (LLM), validated: every referenced evidence id must be real, and at least 2 real ids must survive filtering | recorded as an issue tag; does not by itself force reject (folded into the verdict) |

**Deliberately not implemented**: `scope_mismatch` (no deterministic or
reliably-groundable signal for "is this hypothesis even about the right
incident" was found), and any independent, structural contradiction
*engine* (see **Contradiction handling** below).

## Reflection contract

| Verdict | Meaning | Action |
|---|---|---|
| `accept` | Hypotheses (as generated, or as revised) are adequately supported | Final hypotheses persisted with fixed penalties applied to any individually-flagged hypothesis |
| `revise` | At least one hypothesis has a concrete, fixable problem | Triggers the ONE bounded revision attempt (first pass only) |
| `reject` | Evidence does not support a confident conclusion at all | `hypotheses` become `[]`; a deterministic, honest next-step message is substituted |

A second `revise` verdict (on the already-revised hypothesis set) has
nowhere left to go -- `MAX_REVISION_ATTEMPTS = 1` is exhausted -- and is
treated as `reject`, never as a silent `accept`: the critique itself just
said the revision still isn't right, so accepting it anyway would defeat
the purpose of critiquing at all.

Malformed critique output (invalid JSON, an unrecognized verdict value) is
never silently treated as `accept` -- it raises internally
(`_CritiqueParsingError`), is retried via `call_with_retry`, and on
exhaustion produces `review_status="review_failed"`, never a fabricated
success.

## Architecture

```
Authorized Evidence (already gathered by investigation.evidence,
                      already scoped by actor/SearchFilters)
        v
Hypothesis generation (investigation.hypothesis, one LLM call)
        v
Structural validation (deterministic, no LLM)
        v
  insufficient_information / no hypotheses? --> REJECT (no LLM call spent)
        v
Semantic critique pass 1 (one bounded LLM call)
        v
  accept ---------------------------> apply fixed penalties -> ACCEPT
  reject ---------------------------> REJECT
  revise
        v
  ONE bounded revision (reuses investigation.hypothesis.generate_hypotheses
                         with critique_feedback)
        v
  Structural validation (again, on the revision)
        v
  Semantic critique pass 2 (one more bounded LLM call)
        v
  accept ---------------------------> apply fixed penalties -> ACCEPT
  reject or revise -----------------> REJECT (revision budget exhausted)
        v
Final InvestigationResult (review_status / critique_verdict /
                            revision_count / critique_issues)
        v
Persisted: incident_timeline, event_type="investigation" (unchanged table)
```

## Files changed

| Path | New/Modified | Purpose |
|---|---|---|
| `app/agents/investigation/critique.py` | New | Structural validation, semantic critique LLM call + validation, bounded orchestration (`review_investigation`). |
| `app/agents/investigation/hypothesis.py` | Modified | `generate_hypotheses` gained an optional `critique_feedback` parameter (default `None`, byte-identical prompt when omitted) -- reused for the one revision attempt rather than a second prompt-building code path. |
| `app/agents/investigation/node.py` | Modified | Calls `critique.review_investigation` after hypothesis generation, before persisting; uses the reviewed (possibly revised) hypotheses for both the returned `AskResponse` and the `GraphState` update. |
| `app/shared/schemas/agent_contracts.py` | Modified | `InvestigationResult` gained `review_status`/`critique_verdict`/`revision_count`/`critique_issues`, all defaulted -- every pre-existing construction site unaffected. |
| `app/core/incidents/service.py` | Modified | `record_investigation_result`'s `event_data` dict gained the same four keys (plain JSON values into the existing JSONB column -- no migration). |
| `app/shared/config/settings.py` | Modified | Four new `investigation_critique_*` settings (kill switch + three behavioral thresholds). The hard pass/revision ceiling is deliberately NOT here -- see **Bounded execution**. |
| `app/evaluation/schemas.py` | Modified | `ExpectedCritique`, `ExpectedInvestigation.critique`. |
| `app/evaluation/adapters/generation.py` | Modified | `CritiqueAdapter` protocol, `FixtureCritiqueAdapter`, `RealCritiqueAdapter`. |
| `app/evaluation/runner.py` | Modified | `_run_investigation_case` checks critique expectations when a case declares them; unchanged otherwise. |
| `app/evaluation/fixtures/canned_generations.py`, `fixtures/investigation_core_v1.jsonl` | Modified | 5 new critique-focused cases (accept, reject, review_failed, revise-then-accept, one regression control). |
| `tests/agents/investigation/test_critique.py` | New | Full unit coverage (25 tests): structural validation, malformed-output handling, penalty application, and end-to-end orchestration (all required negative controls). |

## Evidence and grounding safety

**What the critic receives**: exactly the `hypotheses` and `evidence`
already in memory when `review_investigation` is called -- nothing fetched
new, nothing from memory/graph/proactive findings (see **Memory, graph,
and proactive-findings integration** below).

**What it cannot access**: anything not already in that `evidence` list.
`critique.py` makes no repository call, no retrieval call, and holds no
reference to `session`/`actor`/`Identity` at all -- it is a pure function
of its inputs. Verified directly by
`test_critique_prompt_never_contains_evidence_outside_what_was_passed`.

**Why critique output is not evidence**: `SemanticCritiqueResult` is a
separate Pydantic type, never an `EvidenceItem`. It is never appended to
`state.evidence`, never given a `[n]` citation marker, and never reaches
any citable channel. Its only effects are (a) which verdict/penalty is
applied to existing hypotheses and (b) short category tags recorded in
`critique_issues`.

**How stale/deleted evidence is handled**: not applicable at this stage --
`investigation.evidence.gather_evidence` (upstream, unchanged) is the only
place evidence is fetched, and it already only returns what the actor's
authorization permits at the moment of the call. Critique reasons over
whatever that upstream step produced; it does not re-fetch or re-validate
evidence rows.

**How unauthorized evidence is excluded**: structurally impossible for any
to reach critique, because critique never performs its own retrieval --
see "what it cannot access" above.

## Bounded execution

- **`MAX_CRITIQUE_PASSES = 2`**: one initial pass, and (only if that pass
  says "revise") one more pass validating the single revision.
- **`MAX_REVISION_ATTEMPTS = 1`**: hypotheses may be regenerated at most
  once in response to critique feedback.
- **Both are Python module constants in `critique.py`, deliberately NOT
  `Settings` fields** -- no configuration change, accidental or malicious,
  can widen them into an unbounded loop. Only the softer behavioral
  thresholds (whether critique runs at all, the evidence-count floor, the
  overconfidence threshold and its citation-count pairing) are
  configurable.
- **Why a loop is structurally impossible, not just bounded by value**:
  `review_investigation`'s body contains no loop construct of any kind --
  no `while`, no recursion. It is a fixed, linear sequence of `if`/`else`
  branches; the worst case (revise, then a second revise) is read directly
  off the function's source, not computed from a counter that could be
  misconfigured.
- **Failure behavior**: critique-model failure or malformed output ->
  `review_status="review_failed"`, original hypotheses preserved (never
  discarded, never silently upgraded to "reviewed"). Revision-generation
  failure -> `review_status="review_failed"`, pre-revision hypotheses
  preserved, `critique_verdict="revise"` reported honestly (the last
  verdict that actually completed). A disabled kill switch, or nothing to
  critique (zero hypotheses already), both yield `review_status=
  "not_reviewed"` -- the pre-Priority-7 behavior, unchanged.

## Authorization and tenant isolation

Critique introduces no new authorization surface because it introduces no
new data access. Organization isolation, permission checks, and evidence
authorization all happen exactly where they already did -- upstream, in
`investigation.evidence.gather_evidence`, scoped by the calling `actor`'s
`Identity`/`SearchFilters`. `critique.review_investigation` never receives
an `Identity`, a `session`, or an `organization_id`; it cannot express a
cross-tenant or unauthorized-evidence access even if it tried, because
there is no parameter through which to request one. Cross-tenant/
permission-isolation negative controls are therefore verified as a
structural property of the module's signature and by the evidence-boundary
test above, rather than by a runtime authorization check that could be
disabled or bypassed.

## Memory, graph, and proactive-findings integration

None of Priority 4's memory, Priority 5's knowledge graph, or Priority 6's
proactive findings are wired into critique. This was a deliberate choice,
not an oversight: `review_investigation` only ever receives the
`evidence`/`hypotheses` the existing Investigation Agent pipeline already
produced. Memory and graph relationships are not citable evidence in this
codebase (`core.memory`/`core.graph`'s own module docstrings establish
this), and a proactive finding is a derived interpretation, not grounding
evidence (`core.proactive`'s own module docstring, Priority 6). Feeding any
of them into critique would mean critique reasoning over something that
was never validated as evidence in the first place -- exactly what this
priority's spec forbids. If a future priority finds a genuine use case, it
requires its own non-citable context channel and its own evaluation, the
same shape Priority 4 built for the Answer Agent's `memory_context` -- not
attempted here.

## Contradiction handling

There is no independent contradiction-detection engine. The only
contradiction signal `SemanticCritiqueResult.contradictory_evidence` can
ever carry is what the critique model itself reports having observed
directly in the evidence text it was given for that one call -- and even
that is validated before being trusted: `_filter_contradictions` drops any
note referencing an evidence id that isn't real, and drops any note left
with fewer than two real ids (a contradiction needs two things to
conflict). This means the system can explain, at a structured level, which
hypothesis is affected and which real evidence ids the critique considered
conflicting -- but it cannot proactively discover a contradiction the
critique wasn't already looking at in that one pass, and it never claims
to. Broader, cross-evidence contradiction analysis was evaluated and
explicitly deferred (see **Remaining limitations**).

## What gets persisted

`InvestigationResult`'s four new fields, written into the same
`incident_timeline.event_data` JSONB dict `record_investigation_result`
already produces:

- `review_status`: `"not_reviewed"` / `"reviewed"` / `"review_failed"`.
- `critique_verdict`: `"accept"` / `"revise"` / `"reject"` / `null`.
- `revision_count`: `0` or `1`.
- `critique_issues`: short structured category tags only (e.g.
  `"overconfidence:hypothesis_0"`, `"unsupported_claim:hypothesis_1"`,
  `"contradictory_evidence"`) -- never a critique transcript, never raw
  model reasoning. `SemanticCritiqueResult.revision_guidance` (the one
  prose field the critique produces) is used only in memory, to steer the
  single revision prompt, and is never written to `critique_issues` or
  anywhere persisted.

## Cost and observability

- **Reused, not duplicated**: critique's LLM calls go through the exact
  same `llm` object already threaded into `make_investigation_agent_node`.
  The `UsageMetadataCallbackHandler` `agents.service._run_graph_and_record`
  already attaches to the whole graph execution captures critique's token
  usage automatically -- zero additional wiring, and critique spend is
  already covered by the existing organization-level `check_cost_budget`
  pre-flight gate.
- **Structured logging, the only existing per-node observability
  mechanism**: `investigation_critique_completed`/`investigation_critique_
  exhausted`/`investigation_revision_exhausted` events, matching the exact
  keyword-argument `structlog` convention every other agent stage already
  uses. No new tracing infrastructure was introduced -- repository
  discovery confirmed no per-node OpenTelemetry spans exist anywhere in
  `app/agents/` today, so adding one for critique alone would have been
  new, unprecedented infrastructure, not a reuse of something established.

## Test and evaluation results

- **864 backend tests passing** (up from 839 before this priority; +25, all
  new, in `tests/agents/investigation/test_critique.py`). 7/7
  import-linter contracts kept. No schema change, so the Alembic head is
  unchanged.
- Unit coverage includes every required negative control: unsupported
  hypothesis (rejected), supported hypothesis (accepted without
  unnecessary revision), the revision bound (a second `revise` verdict
  enforced as `reject`), malformed critique output (never silent
  acceptance), critique failure (degrades to `review_failed`, original
  preserved), the evidence boundary (critique sees only what was passed
  in, proving cross-tenant/permission isolation structurally), and
  insufficient evidence (rejected before spending a critique LLM call).
- Evaluation harness extended, no second runner: `ExpectedInvestigation.
  critique` plus a `CritiqueAdapter`, wired into the existing
  `investigation` category (not a new category -- critique is a quality
  gate on investigation output, the same domain that category already
  evaluates). 5 new dataset cases in `investigation_core_v1.jsonl`,
  including one deliberately-wrong-expectation regression control. `uv run
  python scripts/run_evaluation.py` -> **56 cases (up from 51), 0
  regressions, VERDICT CLEAN**, exit 0.
- Only local verification was performed -- no claim of a hosted GitHub
  Actions run.

## Remaining limitations

- **Production-calibrated thresholds**: `investigation_critique_
  overconfidence_threshold` (0.75), `investigation_critique_min_evidence_
  per_hypothesis` (2), `investigation_critique_min_evidence_count` (2), and
  the fixed confidence penalties (`_OVERCONFIDENCE_PENALTY = 0.2`,
  `_UNSUPPORTED_CLAIM_PENALTY = 0.3`) are initial, reasoned values, not
  empirically calibrated against production data.
- **Advanced/cross-evidence contradiction analysis**: deferred -- see
  **Contradiction handling** above. Only what one critique pass directly
  observes, in that one call, is ever reported.
- **Multi-step autonomous reflection**: explicitly not built -- the ceiling
  is one revision, structurally, not a configurable depth.
- **Human review workflow**: no UI or human-in-the-loop acceptance/
  override of a critique verdict exists.
- **User feedback learning**: nothing about critique's behavior adapts
  from past outcomes; every run is independent.
- **Long-term critique analytics**: no dashboard or aggregate reporting
  over `review_status`/`critique_verdict` history was built (the data is
  in `incident_timeline` and queryable, but no new aggregation surface
  exists).
- **Semantic live benchmark**: no evaluation of critique's actual judgment
  QUALITY against a real corpus was performed -- that requires a funded
  model API and live data, and belongs in this project's existing
  integration/live evaluation tier if pursued later, per that tier's own
  established scope.
