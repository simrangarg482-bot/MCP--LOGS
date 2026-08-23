# Reliability, rate limiting, and cost controls (Phase 6)

## External service timeouts

| Dependency | Timeout | Where |
|---|---|---|
| OpenAI (`ChatOpenAI`) | 60s | `app/agents/llm.py` -- previously an **actively disabled** timeout (`request_timeout=None` is forwarded literally to the SDK, which only substitutes its own default for the `NotGiven` sentinel, not `None`) -- a real, confirmed gap, not a hypothetical one. Fixed this phase. |
| PostgreSQL (per-statement) | 30s | `app/database/session.py`'s `connect_args["command_timeout"]` -- previously entirely absent; only the connection-establishment timeout (asyncpg's own 60s default) was bounded, not query execution. |
| PostgreSQL (pool recycle) | 30 min | `app/database/session.py`'s `pool_recycle` -- bounds staleness of pooled connections independent of `pool_pre_ping`'s reactive check. |
| Redis/arq | arq's own defaults (1s/attempt, 5 retries) | Unconfigured, using `arq.connections.RedisSettings` defaults -- adequate, not changed this phase. |
| Ingestion connectors (GitHub/Slack/Jira/Confluence/Teams/SharePoint/Azure DevOps) | 30s | Already consistent across all 6 real connectors (`httpx.AsyncClient(..., timeout=30.0)`) -- confirmed via audit, no change needed. |
| Azure Key Vault | Azure SDK defaults | Not configured explicitly -- lower priority given `app/shared/security/kms.py`'s own docstring notes this is never on a per-request hot path. Not changed this phase. |

## Retry strategy

Three retry sites, all now using **full jitter** (`app.shared.backoff.full_jitter_backoff_seconds`) instead of a bare `min(2**attempt, cap)` -- previously, a correlated failure (an OpenAI-wide outage, a Redis blip) meant every affected job/request retried at the exact same intervals, arriving back in synchronized waves:

- `app.ingestion.workers.tasks._schedule_retry` -- capped at 300s, `max_tries=3`.
- `app.agents.workers.tasks.run_knowledge_gap_detection_task` -- same shape, `max_tries=3`.
- `app.agents.retry.call_with_retry` -- capped at 2s, up to 2 retries, wraps every LLM/vector-store/live-evidence call inside agent graph nodes.

None of these had an infinite-retry risk (all confirmed bounded before this phase) -- jitter was the real, confirmed gap, not the bound itself.

**Worker `job_timeout` asymmetry fixed**: `app.ingestion.workers.main` explicitly set 1800s; `app.agents.workers.main` had silently fallen through to arq's 300s default despite an analogous "could scale with organization volume" concern. Now explicitly 600s, with reasoning documented inline for why it's not simply copied from ingestion's value.

**Ingestion `job_timeout` raised 1800s → 3600s (real-world follow-up)**: a real full sync against an actual GitHub connector hit the 30-minute ceiling and was killed mid-embedding, losing the entire (transactional) sync. Root cause: `app.ingestion.connectors.github`'s full sync runs four phases per repo (files, commits, pulls, issues), each embedding synchronously via a local, CPU-bound `sentence-transformers` model (`app.retrieval.embedding`) -- one call per document, on whatever CPU the machine has free. 1800s was a reasoned estimate; 3600s is a measurement-informed adjustment, not a guarantee -- an even larger repo (or slower hardware) can still exceed it. The real fix for that ceiling is stage-level resumability (retrying from where a sync left off, not from the top), explicitly flagged as a separate, larger undertaking in `_execute_ingestion_job`'s own docstring, not attempted here.

**Known, disclosed nuance** (not fixed, documented): `run_ingestion_job_task` redelivery is data-idempotent (content-hash dedup prevents duplicate `Document`/chunk rows) but not audit-row-idempotent (a redelivered job creates a second `ingestion_jobs` row). Low-severity, not addressed this phase.

## Rate limiting (Phase 6.5)

`TokenBucketRateLimiter` relocated from `app.ingestion.rate_limiter` to `app.shared.rate_limiter` -- `app.api`/`app.core` are both import-linter-forbidden from depending on `app.ingestion`, and the class itself has no ingestion-specific logic (only its ingestion callers did). Gained a non-blocking `try_acquire()` mode (the original `acquire()` blocks, correct for background jobs; an HTTP request path must fail fast with 429, never hang).

**Real bug found and fixed during this work**: the class's burst-capacity formula (`capacity = max(rate, 1.0)`) made sense for `app.ingestion`'s per-*second* rates, but collapses to an almost-useless 1-token burst for human-facing per-*minute* rates (e.g. "20/minute" → `rate ≈ 0.33/s` → burst of 1, refilling one token every 3 seconds). Fixed by letting callers pass an explicit `capacity` decoupled from `rate` — and separately fixed the bucket's cold-start initialization, which still handed a brand-new key only `min(rate, capacity)` tokens even with an explicit high capacity, defeating the fix's own purpose. Both are covered by regression tests (`tests/shared/test_rate_limiter.py`).

New `app/api/rate_limit.py` — three dependency factories (`rate_limit_by_ip`/`rate_limit_by_user`/`rate_limit_by_org`), composed via FastAPI's `dependencies=[Depends(...)]`, not a blanket middleware (identity isn't known until `get_current_identity` already ran, and different endpoints need different dimensions):

| Endpoint | Dimension | Limit | Why |
|---|---|---|---|
| `POST /auth/login` | IP | 10/min | Previously **zero** protection against credential-stuffing/brute-force login attempts. |
| `POST /auth/signup` | IP | 10/min | Spam-account-creation protection. |
| `POST /ask` | user | 20/min | Each human's own budget, independent of org size. |
| `POST /incidents/{id}/investigate` | user | 10/min | Heavier than `/ask` -- runs the full Investigation Agent graph. |
| `POST /search/similar-incidents`, `POST /search/recent-changes` | user | 30/min | Lighter than `/ask`/`/investigate`. |
| `POST /tenancy/connectors/{id}/sync` | organization | 10/min | Aggregate org load on the shared ingestion queue/connector budgets, not per-user. |

**In-process only**, same disclosed limitation `app.ingestion`'s own budgets already carry: multiple API replicas would each enforce an independent view, multiplying the real ceiling by replica count. A Redis-backed distributed limiter is the correct production fix once this application runs more than one replica -- flagged as follow-up, not silently assumed solved.

## AI cost budget enforcement (Phase 6.6)

Phase 5 built *telemetry* (recording actual token usage after the fact); this phase adds *enforcement* (blocking a further LLM call before it starts). `app/agents/cost_budget.py::check_cost_budget`, checked at the top of `answer_question`/`triage_incident` (via `_run_graph_and_record`), `generate_postmortem`, and `detect_knowledge_gaps` -- before an `agent_executions` row is even created for the attempt.

- Controlled by `Settings.max_organization_cost_usd_per_day` -- **unset by default, meaning no enforcement**. This codebase does not invent a "reasonable" dollar cap for a deployment it knows nothing about.
- Organization-scoped (not per-user or global) -- matches `app.ingestion.rate_limiter`'s existing per-organization budget convention for an analogous reason.
- Rolling 24-hour window, computed from real `agent_executions.prompt_tokens`/`completion_tokens` (Phase 5), priced via the same `get_estimated_cost_usd` pricing table telemetry already uses -- an estimate, never real OpenAI billing data.
- Fails open on ambiguity: no budget configured, no usage yet, or an unpriced model all mean "allow the call," never "block by default."
- Raises `CostBudgetExceededError` (429, distinct `error_code` from `RateLimitedError` -- a spend ceiling and a request-frequency ceiling are different problems with different remedies) as a real, expected domain error that propagates to the caller, not a fabricated degraded answer.

## Not done this phase (disclosed, not silent)

- **6.7 Performance benchmarking**: not run. Requires a live database/Redis/OpenAI to produce meaningful numbers against real latency -- this environment has none (see `docs/operations/migration-recovery.md`'s disposable-database blocker, unresolved).
- **6.8 Failure injection**: partially covered by existing tests (`tests/api/test_health.py` mocks DB/Redis unavailability at the check-function level; `app.api.main._lifespan`'s Redis-startup-failure handling is exercised). Real socket-level failure injection (an actual connection refusal, an actual OpenAI timeout) was not attempted -- same environment blocker.
- Azure Key Vault client timeout -- flagged, not configured (low severity, not a hot-path call).
- Distributed (Redis-backed) rate limiting -- flagged as the correct fix once this application runs multiple replicas; in-process is correct for the current single-replica deployment shape.
