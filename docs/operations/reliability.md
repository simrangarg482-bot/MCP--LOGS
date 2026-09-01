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

**Ingestion timeout recovery**: measured full GitHub syncs exceeded both the original 30-minute ceiling and, on a resource-constrained Windows machine using remote Neon, the later one-hour ceiling. The hard ceiling remains bounded and configurable with `INGESTION_JOB_TIMEOUT_SECONDS` (7200s default), but it is no longer the primary progress mechanism. After every fully persisted connector page, `_execute_ingestion_job` stores a versioned cursor, fixed incremental watermark, connector-config fingerprint, and timestamp. A timeout/retry resumes that cursor; an expired checkpoint, changed connector configuration, or different full/incremental mode invalidates it and restarts safely. When a replacement attempt begins, any predecessor left `running` by a worker/network death is atomically closed as `failed/WorkerInterrupted`. Checkpoint age is bounded by `INGESTION_CHECKPOINT_TTL_SECONDS` (24h default).

**Redis worker-process recovery**: use `python scripts/run_ingestion_worker.py`, including in Docker and Render. Redis command retries handle ordinary dropped commands, but Redis cannot transparently replay a lost `WATCH` transaction; stock ARQ lets that `WatchError` terminate its polling loop. The resilient entrypoint catches only Redis connection/timeout/watch failures at the poll boundary, applies bounded exponential backoff, and preserves running job tasks in the same worker process.

**Worker saturation and duplicate execution**: ingestion worker concurrency is explicit (`INGESTION_WORKER_MAX_JOBS`, default 2) instead of ARQ's resource-heavy default of 10. A Redis token-guarded connector lock prevents a manual sync, reconciliation tick, or second worker replica from processing the same connector concurrently. The lock has a TTL beyond the hard job ceiling and uses compare-and-delete release, so a dead process cannot leave a permanent lock and one worker cannot release another's lock.

**Provider throttling/outages**: connector reads retry transport failures and HTTP 408/425/429/500/502/503/504. `Retry-After` is honored with a 60s cap; permanent 4xx responses still fail immediately.

**Known nuance**: ARQ redelivery after a real failed attempt creates a separate `ingestion_jobs` history row, while document/chunk writes remain content-hash idempotent. Concurrent duplicate deliveries are suppressed by the Redis connector lock. After the third failed attempt, the last run becomes `dead_lettered` and must be explicitly replayed with `POST /tenancy/connectors/{connector_id}/runs/{job_id}/replay`; exhausted work is never retried forever.

## Ingestion resource guards

Every connector is subject to hard, configurable attempt limits before untrusted provider data can exhaust a worker:

| Setting | Default | Protects against |
|---|---:|---|
| `INGESTION_MAX_PAGES_PER_ATTEMPT` | 10,000 | cyclic or effectively unbounded pagination |
| `INGESTION_MAX_ITEMS_PER_PAGE` | 2,000 | unexpectedly oversized provider responses |
| `INGESTION_MAX_DOCUMENT_BYTES` | 10 MB | single-document memory spikes |
| `INGESTION_MAX_CHUNKS_PER_DOCUMENT` | 1,000 | pathological chunk/embedding amplification |

The shared connector contract also rejects `has_more=true` without a cursor and repeated cursors. These failures are recorded as `IngestionSafetyLimitError`, are visible in run history without leaking exception text, and follow the same bounded retry/dead-letter policy.

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

The human-facing API limits remain process-local. Ingestion provider budgets are now Redis-backed: the Lua token-bucket operation uses Redis server time and atomically refills/consumes connector and organization buckets, so adding worker replicas does not multiply outbound provider traffic. If an ingestion task is invoked outside ARQ without Redis context (principally unit tests and direct maintenance calls), it deliberately falls back to the existing in-process limiter.

## Embedding and persistence throughput

Sentence-transformer inference runs on a dedicated bounded executor (`EMBEDDING_WORKER_THREADS`, default 1) instead of the event loop's shared executor. A timed-out encode cannot spawn an unbounded number of replacement threads, and `EMBEDDING_BATCH_SIZE` controls the model batch. Chunk vectors are persisted with one multi-values PostgreSQL upsert per document/collection instead of one round trip per chunk. This is process isolation inside the ingestion worker, not an independently autoscaled embedding service; deployments that need GPU-backed horizontal embedding scale should split that boundary in a later architecture change.

## AI cost budget enforcement (Phase 6.6)

Phase 5 built *telemetry* (recording actual token usage after the fact); this phase adds *enforcement* (blocking a further LLM call before it starts). `app/agents/cost_budget.py::check_cost_budget`, checked at the top of `answer_question`/`triage_incident` (via `_run_graph_and_record`), `generate_postmortem`, and `detect_knowledge_gaps` -- before an `agent_executions` row is even created for the attempt.

- Controlled by `Settings.max_organization_cost_usd_per_day` -- **unset by default, meaning no enforcement**. This codebase does not invent a "reasonable" dollar cap for a deployment it knows nothing about.
- Organization-scoped (not per-user or global) -- matches `app.ingestion.rate_limiter`'s existing per-organization budget convention for an analogous reason.
- Rolling 24-hour window, computed from real `agent_executions.prompt_tokens`/`completion_tokens` (Phase 5), priced via the same `get_estimated_cost_usd` pricing table telemetry already uses -- an estimate, never real OpenAI billing data.
- Fails open on ambiguity: no budget configured, no usage yet, or an unpriced model all mean "allow the call," never "block by default."
- Raises `CostBudgetExceededError` (429, distinct `error_code` from `RateLimitedError` -- a spend ceiling and a request-frequency ceiling are different problems with different remedies) as a real, expected domain error that propagates to the caller, not a fabricated degraded answer.

## Not done this phase (disclosed, not silent)

- **6.7 Performance benchmarking**: the repeatable procedure and acceptance gates are defined in `docs/operations/ingestion-runbook.md`. Baseline numbers still have to be captured in the target environment because provider, database, Redis, and CPU latency determine the meaningful result.
- **6.8 Failure injection**: automated tests cover provider timeouts/429s, database disconnect retries, cancellation, lock failures, cyclic pagination, and final-attempt dead-lettering. The target-environment socket/process drills are defined in `docs/operations/ingestion-runbook.md`; they are intentionally not claimed as executed against infrastructure that was not available here.
- Azure Key Vault client timeout -- flagged, not configured (low severity, not a hot-path call).
- Human-facing API rate limiting remains process-local; ingestion worker rate limiting is distributed through Redis.
