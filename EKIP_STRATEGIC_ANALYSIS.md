# EKIP Strategic Analysis
### From RAG Application to Enterprise AI Agent Platform

*Prepared as a principal-engineer / hiring-manager / startup-CTO review of the EKIP codebase, grounded in the actual implementation (core modules, agent graph, retrieval pipeline, MCP layer, REST layer, ingestion connectors, and documentation) — not a generic template.*

---

## 0. Grounding: what's real vs. what's aspirational

Before the analysis, an honest baseline, because it changes what every recommendation below actually means. You asked me to evaluate EKIP as if fully complete — I'll do that — but "complete" needs a factual floor to build on:

**Genuinely real and solid:** multi-tenant RBAC with `_ensure_same_organization` enforced at every service boundary, real OIDC/PKCE SSO flow (not a toy login form), a LangGraph agent graph with confidence-gated routing, a verified-evidence-vs-generated-hypothesis split in the Investigation Agent, hybrid dense+BM25+RRF retrieval, **cross-encoder reranking actually implemented** (`app/agents/retrieval/reranking.py`, not just planned), a dual REST+MCP interface sharing one identity/permission model, and import-linter-enforced module boundaries as executable architecture (rare in portfolio projects).

**Real gaps in the design itself, independent of "finish the milestones"**: no observability/tracing beyond structured logs, no metrics endpoint, no Docker/CI, no evaluation harness for RAG or agent quality, no caching beyond the job queue, no knowledge graph, no cross-session agent memory, no rate limiting, no connection-pool/scaling configuration, and `shared/security` (secret encryption) is a documented stub that passes credentials through unchanged. These aren't "not done yet" — they're not *designed* yet, which matters because Section 1 asks what a sharp reviewer would notice even in a "finished v1."

I'm citing this once, here, so the rest of the document can be direct without re-litigating it every paragraph.

---

## 1. Current Project Evaluation

### As a startup product
EKIP solves a real, expensive problem (incident MTTR, tribal knowledge loss, "who do I even ask") with a design that would actually survive contact with an enterprise buyer's security review — multi-tenancy and RBAC are foundational, not bolted on. What it's missing to *be* a startup: a wedge. Right now it's "a smarter internal search + an investigation assistant." Every enterprise-AI-search competitor (Glean, Hebbia, internal Slack-GPT tools) claims the same thing. The commercially differentiated version isn't "answer questions about our code" — it's "notice things before you ask," which nothing in the current design does (everything is pull, nothing is push).

### As enterprise engineering
This is the strongest lens for EKIP. The tenant-isolation discipline (repeated `_ensure_same_organization` guards, deliberately not yet extracted to avoid premature abstraction, flagged explicitly each time), the import-linter contracts turning "agents may not import ingestion" from a code-review convention into a CI-enforced fact, and the `EKIPError → status_hint` pattern that makes REST and MCP share identical error semantics — these are the things a Staff Engineer actually checks for, and they're there. The weak spot: there is no operational story. An enterprise buyer's next question after "is my data isolated" is "what happens at 3am when this breaks," and today the honest answer is "someone reads structured JSON logs." No tracing, no metrics, no alerting, no runbook for the platform itself (ironic, for an incident-intelligence platform).

### As AI research
This is the weakest lens, and it should be — EKIP isn't a research project, it's a systems/orchestration project, and that's fine as long as it doesn't pretend otherwise. There's no model training, no fine-tuning, no benchmark, no ablation, no published accuracy number anywhere. The "confidence" in confidence-gated routing is a placeholder weighted formula with a hardcoded `0.6` threshold that nothing has calibrated against real outcomes — it *looks* rigorous (there's a whole `confidence_signals` dict for debuggability) but nothing closes the loop to prove the number means anything. If you want any research credibility at all, the cheapest fix is an evaluation harness with a real, if small, labeled dataset and a reported number — see Section 5.

### As a resume / portfolio project
Better than 95% of "I built a RAG chatbot" projects, for a specific reason: most portfolio RAG projects are a script that embeds some PDFs and calls an LLM. EKIP has an actual system — multi-tenant, permission-checked, dual-protocol (REST *and* MCP, which almost nobody else building a portfolio project even knows exists yet), with a real agent graph and a real ingestion pipeline. That's a systems-engineer story, not a "I called an API" story.

**What's already impressive:**
- The **sub-stage A/B evidence separation** (verified evidence collection, then a *separately validated* hypothesis-generation stage that rejects any hypothesis without a supporting evidence reference) — this is a real, uncommon hallucination-containment pattern most people don't implement even when they know they should.
- **Import-linter contracts as architecture-as-code.** This alone signals "this person thinks about systems at scale," not "this person got a demo working."
- The **hybrid live + indexed evidence** design (query the knowledge base first, only hit live GitHub/Slack APIs when confidence is low or freshness matters) — a genuinely reasoned cost/latency/freshness tradeoff, not "always call everything."
- **MCP as a first-class protocol**, sharing business logic with REST rather than being a bolted-on demo. Most people building MCP servers today are wrapping a single script; EKIP treats it as a peer transport to a real API.

**What looks like a normal/student project:**
- No tests running in CI, no CI at all. A project this architecturally disciplined with zero automated verification is a strange, noticeable mismatch — it signals "I know what good looks like" without "I proved it."
- No observability. For an *incident intelligence* platform to have no tracing of its own agent decisions is the single most ironic gap a sharp interviewer will catch immediately.
- Confidence scoring that "looks" real (a dict of signals!) but is an untuned placeholder — this is the kind of thing that reads as sophisticated until someone asks "what's your precision/recall on routing decisions" and there's no answer.

**What has real industry value:**
- The whole tenancy/auth/RBAC/audit stack — this is literally the hard, unglamorous 60% of building enterprise software, and it's the part almost every portfolio project skips because it's not "AI."
- The connector architecture (pluggable `Connector` protocol, cursor-based pagination, idempotent upsert via content hash) — this is real data-engineering, and it transfers directly to any enterprise-integration job.

**What weaknesses will recruiters notice:**
- No numbers. Zero. Every claim ("confidence evaluation," "grounding verification," "hybrid retrieval") is architecturally real but has no measured outcome attached. A recruiter who's seen real production AI systems will ask "how do you know it works" and the honest current answer is "the code is structured to make it possible to know, but nothing measures it yet."
- No deployment story (no Docker, no CI, no infra-as-code) for a project whose entire pitch is "enterprise-ready."
- Nothing autonomous or proactive. Every capability is a response to a question. Enterprise AI in 2026 is judged partly on whether it *initiates*.

---

## 2. Next-Level AI Features

Each idea below states the problem, why a company would pay for it, why it's technically nontrivial, how it plugs into the *existing* EKIP architecture specifically (not generically), complexity, and priority.

### 2.1 Agent memory (episodic + semantic), not just execution logs
**Problem:** every `answer_question`/`triage_incident` call starts from zero. The system has answered "why does checkout timeout" fifty times and remembers none of it except as an opaque `agent_executions` row nobody queries at answer-time.
**Why companies care:** an assistant that gets *sharper* the longer you use it is the entire pitch of "enterprise memory" (this is what Glean/Notion AI are racing toward). Without it, EKIP is stateless intelligence — smart but goldfish-brained.
**Why technically impressive:** distinguishing episodic memory (this org's specific incident history), semantic memory (facts learned about this org's systems — "the checkout service is owned by team X and depends on Redis"), and procedural memory (runbooks that worked before) is a real memory-architecture decision, not "add a vector store."
**Integration:** `agent_executions` already has the raw material (`input_summary`, `confidence_score`, per-org scoping). Add a `agent_memory` table (organization-scoped, kind=episodic/semantic/procedural), populate it as a side effect of `_run_graph_and_record`, and add a new evidence source in `evidence.py` (a seventh source, following the exact pattern the live-evidence sources already established) that queries memory before/alongside retrieval.
**Complexity:** Medium-high (needs its own consolidation job — raw executions aren't memory, they need summarization — which is itself a small agent).
**Priority:** High. This is the single highest-leverage "feels like a real AI product" feature given how much of the plumbing already exists.

### 2.2 Real evaluation framework (the credibility multiplier)
**Problem:** nothing in EKIP proves the confidence score, the grounding check, or the retrieval quality actually work. Every AI feature is a claim, not a measurement.
**Why companies care:** this is *the* differentiator between "someone who built a RAG demo" and "someone who ships production AI" — production AI teams live and die by eval harnesses, because LLM behavior drifts with every prompt/model change and you need a regression gate.
**Why technically impressive:** a real harness means a golden dataset (even 100-200 hand-labeled Q/A pairs with expected citations), an LLM-as-judge scoring rubric (groundedness, relevance, hallucination rate), and a way to run it against every graph change — genuinely close to how OpenAI/Anthropic-adjacent eval teams work, at small scale.
**Integration:** `app/agents/graph.py`'s `build_graph`/`build_investigation_graph` already take a session + LLM — an eval script just calls `answer_question` in a loop against the golden set and scores `AskResponse.citations`/`confidence` against expected answers. Store results in a new `eval_runs` table; surface a trend line.
**Complexity:** Medium. The hard part is authoring the golden dataset, not the harness code.
**Priority:** Highest. Do this before almost anything else in this document — it's what makes every other AI claim credible, and it's the one thing in Section 6 that changes a hiring manager's read on you the most.

### 2.3 AI observability / decision tracing
**Problem:** when the Investigation Agent produces a wrong hypothesis, there is currently no way to see *why* — which evidence was retrieved, what the LLM prompt actually contained, why confidence routed the way it did.
**Why companies care:** "explainability" isn't a nice-to-have for incident intelligence, it's the whole value proposition — an SRE will not trust a root-cause suggestion they can't inspect.
**Why technically impressive:** full trace propagation through a LangGraph pipeline (span per node: retrieval, confidence eval, investigation sub-stage A, sub-stage B) with prompt/response capture is genuinely fiddly to get right without massive overhead.
**Integration:** `opentelemetry-sdk` is already a dependency doing nothing — wire it for real. Wrap each node in `agents/graph.py` in a span; export to a self-hosted Jaeger/Tempo or a hosted LLM-observability tool (Langfuse/Phoenix). This single feature also closes the "no CI/observability" weakness from Section 1.
**Complexity:** Medium.
**Priority:** High — pairs directly with 2.2 (you can't build a good eval harness without also being able to see what the agent actually did).

### 2.4 Model routing + cost optimization
**Problem:** every LLM call in EKIP goes through one `get_llm()` — query rewriting, hypothesis generation, and postmortem drafting all presumably use the same (expensive) model.
**Why companies care:** cost is *the* recurring line item enterprise buyers interrogate for any LLM product; "we route cheap tasks to cheap models" is a mature-team signal and a real margin lever.
**Why technically impressive:** routing isn't just "use gpt-4o-mini for small tasks" — real routing considers task type (classification-like query rewriting vs. open-ended hypothesis generation), fallback on rate-limit/timeout, and cost/quality tradeoffs logged per call.
**Integration:** `agents/llm.py`'s `get_llm()` becomes `get_llm(task: Literal["rewrite","confidence","hypothesis","postmortem"])`, returning a model per task tier; log `model_used` + token counts onto `agent_executions` (the table already exists, just add columns).
**Complexity:** Low-medium.
**Priority:** Medium-high (cheap to build, very visible in an interview: "here's my per-query cost dashboard").

### 2.5 Semantic caching
**Problem:** two users asking "why is checkout down" thirty seconds apart both trigger full retrieval + rerank + generation.
**Why companies care:** latency and inference cost both drop directly; this is a standard production RAG optimization interviewers specifically probe for ("have you thought about caching?").
**Why technically impressive:** semantic (embedding-similarity) cache invalidation is more interesting than key-value caching — deciding *when* a cached answer is stale (a new commit landed, a new Slack message arrived) ties directly into EKIP's own freshness-window logic already built for live evidence (`_LIVE_EVIDENCE_FRESHNESS_WINDOW`).
**Integration:** Redis is already a dependency (currently only for arq). Add a cache keyed on `(organization_id, query_embedding_bucket)`, invalidated on the same `scheduled_reconciliation` cron that already re-syncs connectors — one hook, not a new subsystem.
**Complexity:** Medium.
**Priority:** Medium.

### 2.6 Enterprise knowledge graph
**Problem:** EKIP ingests GitHub and Slack as *documents* — it has no model of "this service depends on that service," "this person owns that repo," "this incident happened to the same service as that one three months ago." Everything is text similarity, nothing is structural knowledge.
**Why companies care:** this is the single feature that turns "search over our docs" into "understands our org" — the difference between Glean and a genuine engineering-intelligence platform.
**Why technically impressive:** building an entity graph (services, repos, people, incidents, deployments) from unstructured ingestion output, keeping it current incrementally, and using it to *augment* retrieval (not replace it) is meaningfully hard and exactly the kind of hybrid symbolic+neural system that differentiates senior AI engineers from prompt-engineers.
**Integration:** `app.ingestion.processors` already extracts structured metadata (`author`, `repo`, `channel`) per document — that's graph edges waiting to happen. A new `core/knowledge_graph` (or extend `core/knowledge`) module ingests `document_metadata` rows into a lightweight graph store (even `networkx` + a Postgres edge table is enough at this scale — Neo4j is overkill until proven otherwise, which is itself a good architectural-judgment story).
**Complexity:** High.
**Priority:** High, but Phase 2/3 — needs the ingestion pipeline mature first.

### 2.7 Reflection loops beyond the current grounding check
**Problem:** the Answer Agent already does a grounding check (embedding-similarity, escalating to LLM verification) — genuinely good — but the Investigation Agent's hypothesis generation has no equivalent self-critique step. A hypothesis with a citation isn't necessarily a *good* hypothesis.
**Why companies care:** self-correction is what separates "an LLM wrapper" from "an agent" in every serious AI-engineering rubric right now.
**Why technically impressive:** a critique-and-revise loop (generate hypothesis → adversarial critique pass questioning the causal claim → revise or discard) is a real reasoning-system pattern, not a prompt tweak.
**Integration:** add a `critique_hypotheses` step to sub-stage B in `agents/investigation/` before hypotheses are attached to `result.investigation` — reuses the exact same LLM-call infrastructure already in place.
**Complexity:** Medium.
**Priority:** Medium-high.

### 2.8 Real-time / proactive intelligence
**Problem:** everything in EKIP is pull (a human asks). The hourly `scheduled_reconciliation` cron only syncs the knowledge base, it never *notices* anything.
**Why companies care:** this is the single biggest gap versus "why would I pay for this over a good search bar" — proactive detection is the actual product differentiator (see Section 3).
**Why technically impressive:** turning a sync job into a pattern-detection job (recurring error signatures across ingested Slack/GitHub content, without a labeled anomaly dataset) is a genuinely open, portfolio-differentiating problem.
**Integration:** extend `scheduled_reconciliation` (already the one real cron in the system) with a post-sync analysis step that diffs newly ingested content against known incident patterns.
**Complexity:** High.
**Priority:** High for differentiation, but sequence after 2.2 (you need eval to know if "proactive alerts" are actually good, not just noisy).

### 2.9 Multimodal ingestion
**Problem:** real incidents are diagnosed from dashboards, stack traces, and screenshots pasted into Slack — today's Slack connector only reads text.
**Why companies care:** this closes an obvious, visible gap between "toy text RAG" and "handles how engineers actually communicate."
**Why technically impressive:** OCR/vision-model extraction of structured signal (error codes, graph shapes, log excerpts) from images, folded into the same `RawDocument` shape ingestion already normalizes everything into.
**Integration:** extend `SlackConnector.normalize()` to detect image attachments and run them through a vision model into text before the existing pipeline touches it — zero changes needed downstream.
**Complexity:** Medium.
**Priority:** Medium (visible, resume-impressive, but not the highest-leverage item here).

---

## 3. Features Nobody Usually Builds

These are the ideas that move EKIP from "well-built RAG platform" to "I haven't seen a portfolio project do this."

**Autonomous incident investigator with a bounded action loop.** EKIP already has the right bones: `propose_runbook_update` creates a human-gated proposal, `trigger_postmortem_generation` drafts (never auto-publishes). Extend this into a genuine loop: on a new incident, the Investigation Agent doesn't just answer once — it *iterates* (re-queries live evidence as new Slack messages arrive during the incident, updates its hypothesis, and only finalizes when confidence stabilizes or a human intervenes). This is "autonomous but supervised," the exact framing enterprises actually want (nobody wants a fully autonomous agent touching production).

**AI debugging engineer that proposes an actual diff, not prose.** Given a stack trace and the GitHub live-evidence source already fetching recent commits/PRs, go one step further: have the agent walk `git blame` on the failing line (via the GitHub API, already integrated) and propose a concrete patch — surfaced through `propose_runbook_update`'s same human-approval gate, never auto-merged. This is a "wow" demo precisely because it's rare and because the guardrail (never auto-merge) is exactly the kind of responsible-AI framing that impresses both engineers and compliance reviewers simultaneously.

**Cross-incident pattern mining ("this is the 4th time").** The Knowledge Gap Agent design already clusters low-confidence executions — apply the identical clustering machinery to *resolved incidents* instead, surfacing "these 4 incidents over 3 months share a root cause signature" as a standing report, not just a per-question answer. This is the feature that makes engineering leadership (not just individual engineers) care about the product — it's a management-visible insight, which is commercially the difference between a tool and a platform.

**Causal incident timelines, not just chronological ones.** `IncidentTimeline` today is a flat, time-ordered list. A causal graph — this deploy caused this alert caused this Slack thread caused this rollback — reuses the knowledge-graph idea (2.6) applied specifically to one incident's evidence graph, and is a genuinely novel visualization/reasoning artifact most incident tools don't attempt because it requires structured extraction, not just a log.

**Organizational knowledge-decay detection.** Beyond "what's a knowledge gap" (low-confidence questions), track which documents are *cited a lot but haven't been updated in N months* — a rotting-runbook detector. Cheap to build on top of existing retrieval citation data, and a feature no competitor markets explicitly even though every engineering org has this exact pain.

**Agent self-critique via a second, adversarial model call.** Not "reflection" in the generic sense (2.7) but specifically: a *separate*, deliberately skeptical prompt whose only job is to try to falsify the primary hypothesis using the same evidence set. This "red team agent" framing is uncommon, cheap (one extra LLM call), and a strong interview talking point about adversarial robustness in agentic systems.

**Continuous learning from human feedback, done honestly.** Not full RLHF (out of scope for a single engineer) — but a genuinely useful, achievable version: every `approve_postmortem`/`reject_document`/thumbs-down-on-an-answer becomes a labeled example in a preference dataset, which periodically informs prompt selection (few-shot exemplar retrieval from *good* past outputs) rather than model weights. This is realistic, honest about its own limits, and still a real continuous-learning story.

---

## 4. Enterprise-Level Improvements

**Architecture — scalability/reliability/distributed systems:** the arq worker pool needs horizontal scaling awareness (currently one global hourly cron, no per-tenant fairness — a noisy large tenant starves smaller ones; add per-org queue partitioning or priority weighting). Add explicit connection-pool sizing (`pool_size`/`max_overflow` on the async engine) — currently unconfigured, which will silently fall over under real concurrent load. `call_with_retry` already exists for agent-level retries — extend the same backoff discipline to the ingestion connectors' external API calls (GitHub/Slack rate limits are a *guaranteed* production incident otherwise).

**Queues/caching:** arq is well-chosen and already justified in `ENGINEERING_DECISIONS.md`. Add Redis-backed semantic caching (2.5) and a request-level idempotency cache for MCP tool calls (a retried tool call from a flaky client shouldn't re-trigger a live GitHub fetch).

**Observability:** wire the already-declared OpenTelemetry dependency for real (2.3), add a `/metrics` Prometheus endpoint (also already a dependency doing nothing) covering agent latency/confidence distribution/tool-call counts, and add structured alerting on `agent_executions.status = "failed"` rates per org.

**Security — enterprise auth/authorization/data isolation/compliance:** finish `shared/security` (envelope encryption via KMS) before this ever touches real customer secrets — right now `client_secret_ref`/connector credentials are stored as plaintext-equivalent references, which is the single most disqualifying gap for an actual enterprise security review, complete-project framing or not. Add SCIM-based provisioning (enterprises don't want to manually invite users) and consider row-level security (Postgres RLS) as defense-in-depth *underneath* the existing application-level `_ensure_same_organization` checks, not instead of them. For compliance, add exportable audit trails (SOC2 auditors want CSV/API export of `audit_logs`, not just a query function).

**AI — hallucination prevention/evaluation/model routing/cost:** the grounding check is good; extend it with a per-citation verification score (not just sentence-level similarity) surfaced to the end user so they can see *which* claims are strongly vs. weakly grounded. Add the evaluation framework (2.2) and model routing (2.4). Add token-budget enforcement per organization (a runaway agent loop currently has no cost ceiling).

**Backend — API design/database/production readiness:** the REST layer's own honestly-flagged gap (no `X-Total-Count` on list endpoints because no count query exists) should be fixed — pagination without a total is a real API smell. Partition `agent_executions`/`mcp_requests` by time (they're append-only, high-volume, and will bloat). Add health-check endpoints (`/healthz`, `/readyz`) and graceful shutdown handling for the arq workers — currently no deployment story exists for either.

---

## 5. AI Engineering Maturity: RAG Application → Enterprise AI Agent Platform

| Dimension | Today | Upgrade |
|---|---|---|
| **Agent architecture** | Fixed linear graph (retrieve → confidence → answer/investigate) | Add a lightweight planner node for multi-part questions ("compare this incident to last month's" needs two retrievals + a synthesis step, not one pass) |
| **Tool usage** | MCP tools exist but are hand-wired per handler | Standardize tool-selection telemetry — log which tools an agent *considered* vs. *used*, the foundation for real agentic behavior analysis |
| **Memory** | None beyond execution logs | Episodic + semantic + procedural, per 2.1 |
| **Reflection** | Answer Agent has grounding check only | Extend to Investigation Agent hypotheses (2.7), add the adversarial critique pattern (Section 3) |
| **Evaluation** | None | Golden-set + LLM-judge harness (2.2), gating graph changes |
| **Tracing** | Structured logs only | Full OTel span tree per graph node (2.3) |
| **Feedback** | None captured | Thumbs up/down + approve/reject events feeding a preference dataset (Section 3) |

The honest one-line version: **EKIP has the orchestration maturity of an agent platform already — it's missing the *measurement* maturity.** Every remaining upgrade in this table is really the same underlying investment (observability + evaluation) paying off in five different places.

---

## 6. Recruiter Impact Analysis

**Would this get you shortlisted for an AI Engineer / ML Engineer / Backend AI Engineer role?** For backend-leaning AI engineering roles (the kind that build platforms, not train models) — yes, clearly, on architecture alone. The tenant isolation discipline, the import-linter contracts, the dual REST/MCP surface, and the verified-vs-generated evidence split are things I would not expect from most candidates, and they'd generate real interview conversation. For a research-leaning ML role — no, not on its own; there's no modeling work, no benchmark, no number. That's not a flaw in the project's identity (it's honestly not that kind of project) but it means you should frame it correctly on a resume: "Platform/Systems AI Engineer," not "ML Researcher."

**What would make me think "this candidate understands production AI systems, not just tutorials"?** Two things, in order: a real evaluation number ("grounding accuracy improved from 71% to 89% after adding reranking, measured against a 150-question labeled set") and a trace/screenshot showing you can actually *see* what the agent did on a specific failure and explain why. Anyone can describe an architecture in a README. Almost nobody at the portfolio-project level can show a regression-gated eval run or a real trace. That gap, closed, is worth more to your candidacy than every other item in this document combined — say that plainly to yourself before building anything else.

---

## 7. Build Roadmap

### Phase 1 — Highest impact, fastest to complete
| Feature | Approach | Tech | Difficulty | Resume Impact |
|---|---|---|---|---|
| Evaluation harness (2.2) | Golden Q/A set + LLM-judge scoring script + trend storage | Existing LLM client, new `eval_runs` table | Medium | **10** |
| OTel tracing (2.3) | Instrument each LangGraph node with spans | `opentelemetry-sdk` (already a dependency) | Medium | **9** |
| Model routing (2.4) | Task-tiered `get_llm(task=...)` + cost logging | Existing LLM client + new columns on `agent_executions` | Low-Medium | 7 |
| CI pipeline | Run pytest + ruff + mypy + lint-imports on every push | GitHub Actions | Low | 7 |
| Prometheus `/metrics` | Counters/histograms on agent latency, confidence, tool calls | `prometheus-client` (already a dependency) | Low | 6 |

### Phase 2 — Production-grade
| Feature | Approach | Tech | Difficulty | Resume Impact |
|---|---|---|---|---|
| Agent memory (2.1) | `agent_memory` table + consolidation job + new evidence source | Postgres, existing evidence-source pattern | Medium-High | **9** |
| Semantic caching (2.5) | Embedding-bucketed Redis cache with freshness-window invalidation | Redis (already a dependency) | Medium | 6 |
| Reflection/critique loop (2.7) | Adversarial critique pass on hypotheses before surfacing | Existing LLM infra | Medium | 8 |
| Docker + deployment story | Multi-service compose (app, workers, postgres, redis) | Docker | Low-Medium | 6 |
| Secrets management | Real envelope encryption for `credential_ref`/`client_secret_ref` | KMS (cloud-provider or Vault) | Medium | 7 (huge for enterprise credibility) |

### Phase 3 — Research/startup-level differentiation
| Feature | Approach | Tech | Difficulty | Resume Impact |
|---|---|---|---|---|
| Knowledge graph (2.6) | Entity/edge extraction from `document_metadata` into graph queries augmenting retrieval | `networkx` + Postgres edge table (start simple) | High | **10** |
| Proactive/predictive detection (2.8, Section 3) | Post-sync pattern diff against known incident signatures | Existing cron + clustering (reuse Knowledge Gap Agent design) | High | **10** |
| Autonomous bounded-loop investigator (Section 3) | Iterative re-investigation as new evidence streams in during an active incident | Existing graph + live evidence sources | High | 9 |
| AI debugging engineer / diff proposals (Section 3) | Git-blame walk + patch proposal via existing GitHub live source | GitHub API (already integrated) | High | 9 |
| Multimodal ingestion (2.9) | Vision-model extraction folded into existing `RawDocument` normalization | Vision-capable LLM | Medium-High | 7 |

---

## 8. Final Vision

**If EKIP had unlimited runway, it stops being "an internal search tool with agents bolted on" and becomes an Engineering Intelligence Platform — a system that maintains a living, structural understanding of how an engineering organization actually works (its services, its people, its incidents, its failure patterns), not just a searchable archive of what it said.**

The final architecture keeps everything already right about EKIP — the tenancy/RBAC discipline, the verified-vs-generated evidence separation, the dual REST/MCP surface — and adds three structural layers on top:

1. **A knowledge graph substrate** underneath retrieval, so every answer is grounded in both text similarity *and* organizational structure ("this service depends on that one" is a fact, not a guess reconstructed from document co-occurrence).
2. **A measurement layer** (evaluation harness + full tracing) that makes every AI claim in the system provable, not asserted — this is what turns "confidence score" from a placeholder into an actual, defensible metric with a track record.
3. **A proactive layer** that inverts the entire interaction model: instead of an engineer asking a question, the platform notices — a recurring failure signature, a decaying runbook, a service whose on-call load quietly tripled — and *raises its hand*, always through the same human-approval gate already built for postmortems and runbook proposals.

The differentiation strategy against Glean/Hebbia-style enterprise search and against generic "AI SRE" startups is the same in both directions: those products either search (no structural reasoning, no proactivity) or alert (no deep investigation, no verified evidence trail). EKIP's actual, defensible position — the one worth building toward — is the platform that does both, with a receipts-first design where every AI-generated claim can be traced back to a specific piece of verified evidence, measured against a real evaluation set, and never silently promoted to "known fact" without a human in the loop. That combination — proactive, structurally grounded, provably evaluated, and rigorously human-gated — is genuinely rare, and it's the version of this project that stops being a portfolio piece and starts being a company.
