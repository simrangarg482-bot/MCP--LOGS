# Observability (Phase 5)

## Structured logging

`structlog`-based (`app/shared/config/logging.py`), JSON in production,
colored console in development/test. Every log call goes through
`get_logger(__name__)` — never `logging`/`structlog` imported directly
elsewhere.

**Redaction**: `_redact_sensitive_fields` (a structlog processor) redacts
any event-dict key whose name contains `password`/`token`/`secret`/
`api_key`/`authorization`/`credential`/`client_secret`/`private_key`
(case-insensitive substring match, at any nesting depth). A safety net, not
the primary control — every call site already follows this codebase's
existing convention of logging summaries/ids, never raw secrets (see
`AgentExecution.input_summary`'s own docstring). Tested in
`tests/shared/config/test_logging.py`.

## Request correlation

Every request/job carries a `request_id`, bound into structlog's contextvars
so every log line for its duration includes it automatically — no call site
threads it through function signatures by hand.

- **REST**: `app.api.middleware.RequestContextMiddleware` — accepts a
  caller-supplied `X-Request-ID` header or mints one, echoes it back in the
  response, logs one `http_request_completed`/`http_request_failed` event
  per request with method/path/status_code/duration_ms.
- **Identity context**: `app.api.deps.get_current_identity` additionally
  binds `organization_id`/`user_id` once resolved.
- **MCP**: `app.mcp.dispatch.run_mcp_tool` mints its own `request_id` (no
  HTTP header to accept one from) and binds it plus `mcp_tool`/
  `organization_id`/`user_id`.
- **Workers**: `app.ingestion.workers.tasks.run_ingestion_job_task` and
  `app.agents.workers.tasks.run_knowledge_gap_detection_task` bind arq's own
  `job_id` as `request_id`, plus whatever tenant context is known at that
  point (`connector_config_id` / `organization_id`).

Tested in `tests/api/test_middleware.py`.

## Distributed tracing

`app/shared/config/tracing.py` — `FastAPIInstrumentor.instrument_app` auto-
creates one span per HTTP request (method, route, status). Wired into
`app.api.main.create_app`, using dependencies (`opentelemetry-sdk`,
`opentelemetry-instrumentation-fastapi`) that were already in
`pyproject.toml` before this phase but completely unused until now.

**Not done in this pass**: per-stage spans inside retrieval (dense/lexical/
reranking) or the agent graph — `opentelemetry-instrumentation-fastapi` only
instruments the HTTP boundary; hand-instrumenting every internal stage with
`tracer.start_as_current_span(...)` is separate work, not yet started.

**Export**: configuration-gated via `OTEL_EXPORTER_OTLP_ENDPOINT`. **No real
trace backend (Jaeger/Tempo/Azure Monitor/etc.) is deployed for this project
yet** — spans are created (so the instrumentation itself is exercised) but
exported nowhere in production without that setting, and printed to the
console in development only. Do not claim tracing is "live" until a real
collector is configured and this has been verified against it.

## AI usage / cost telemetry

`agent_executions` gained four columns (migration `f1ea4eb67264`):
`model_used`, `prompt_tokens`, `completion_tokens`, `total_tokens`. `NULL`
means "not captured," never "zero spent" — see the model's own column
comments.

Captured via LangChain's `UsageMetadataCallbackHandler`
(`app.agents.telemetry.summarize_usage`):
- `app.agents.service._run_graph_and_record` attaches one handler per
  `graph.ainvoke` call — covers `answer_question`/`triage_incident` and
  every node they run (query rewriting, generation, grounding, sufficiency,
  hypothesis generation) without instrumenting each node individually.
- `generate_postmortem`/`detect_knowledge_gaps` (which call the LLM
  directly, not through the graph) use `llm.with_config(callbacks=[handler])`
  — binds tracking to the LLM instance itself, zero changes needed in
  `run_postmortem_pipeline`/`_run_knowledge_gap_pipeline`'s internals.

**Provenance note**: an earlier, never-merged branch added near-identical
columns to this same table for a similar purpose and left them physically
present (but never wired to any code) on the shared Neon development
database — see `docs/operations/migration-recovery.md`. This is a fresh,
independent implementation with a real, tested code path behind it, not a
resurrection of that work.

**Cost estimation** (`app.agents.telemetry.get_estimated_cost_usd`): from
real token counts × a published OpenAI pricing table — an estimate, never
real billing data. Returns `None` for any unpriced model rather than
guessing. Exposed via `GET /observability/agents`'s `estimated_cost_usd`
field (computed against the *currently configured* model — see that
endpoint's own docstring for the multi-model-history caveat).

## Dashboards

Three permission-gated (`observability:read`) aggregate endpoints, all
following the same shape (count/error/latency, grouped by name):

| Endpoint | Groups by | Source table |
|---|---|---|
| `GET /observability/agents` | agent_name | `agent_executions` |
| `GET /observability/mcp` | tool_name | `mcp_requests` |
| `GET /observability/ingestion` | connector_config_id | `ingestion_jobs` |

`/observability/ingestion` is new this phase — the one dashboard gap found
during the Phase 5 audit (agents and MCP already had one; ingestion had
none). Lives in `core.tenancy.service`/`repository`, not `app.ingestion`
directly — `app.api`/`app.core` are both import-linter-forbidden from
depending on `app.ingestion`, the same constraint `list_ingestion_runs`
(the per-connector history endpoint) already works within.

**Frontend**: `AgentsPage` already consumed `/observability/agents` for
real before this phase. `McpToolsPage` had a real bug this phase fixed — it
rendered `/observability/mcp`'s response (a usage-stats shape:
`tool_name`/`request_count`/`error_count`/`avg_latency_ms`) as if it were a
tool *catalog* (`name`/`description`/`status`/`parameters`), which don't
exist on the real response at all. Fixed by making the catalog-only fields
genuinely optional and mapping only what the real endpoint returns — mock
mode's richer illustrative catalog (with a working "Test Tool" drawer) is
unchanged and still clearly gated behind `VITE_USE_MOCK_DATA`.

## Ingestion telemetry — what's still coarse

`ingestion_jobs` has no retry-count column (a retry re-runs the whole sync
from the top today, per `_execute_ingestion_job`'s own docstring — there's
no per-attempt counter to expose) and no chunk-count column (only
`documents_processed`). The new `/observability/ingestion` dashboard uses
only what already exists (status, timestamps, `documents_processed`) rather
than adding new columns for this pass — a real, disclosed gap versus
agents' telemetry, not silently glossed over.

## Alerting

**Defined here as thresholds/conditions, not wired to a live alerting
backend** — no monitoring infrastructure (Azure Monitor, Datadog,
Prometheus/Alertmanager, etc.) is deployed for this project yet (Azure
deployment itself remains blocked — see `docs/operations/deployment.md`).
Claiming these are "active alerts" would be exactly the kind of unverified
claim this project's own rules prohibit. Once a real backend exists, these
are the conditions to wire up first, using the telemetry this phase already
produces:

| Alert | Condition | Source |
|---|---|---|
| API 5xx spike | `http_request_completed` events with `status_code >= 500` exceed a rate threshold over a rolling window | `RequestContextMiddleware` logs |
| Latency spike | `duration_ms` p95 on `http_request_completed` exceeds a threshold | same |
| Worker failures | ingestion/knowledge-gap task retry/failure log rate exceeds a threshold | `ingestion_job_task_retry_scheduled`-style logs, `/observability/ingestion`'s `failed_count` |
| Redis failures | `arq_pool_unavailable_at_startup` or `readiness_redis_check_failed` log events occur | `app.api.main`/`app.api.routers.health` logs |
| DB failures | `readiness_database_check_failed` log events occur, or `/ready` reports `database.status != "ok"` | `app.api.routers.health` |
| Ingestion failures | `/observability/ingestion`'s `failed_count` for a connector rises over a window | new dashboard |
| LLM failures | `agent_execution_unexpected_failure` log rate, or `/observability/agents`'s `failed_count` | agent execution telemetry |
| Token/cost anomaly | `/observability/agents`'s `estimated_cost_usd` for an organization exceeds a budget threshold over a window | new telemetry (this phase) |
| Authentication anomaly | repeated `permission_denied`/`auth.*` error-code log events from one actor/IP over a short window | `core.users.service.require_permission`'s own logging |

## Known limitations (be explicit, not silent)

- Tracing has no real exporter target deployed — spans exist, nothing
  outside this process sees them yet.
- No per-stage retrieval/agent tracing beyond the HTTP boundary.
- Ingestion telemetry has no retry-count or chunk-count columns.
- Alerting is defined, not wired to any live backend.
- `estimated_cost_usd` is an estimate from published pricing, never real
  OpenAI billing data.
