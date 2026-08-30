# Ingestion production runbook

## Deployment prerequisites

1. Apply Alembic migrations before starting new workers. Confirm `alembic heads` reports one head and the database is at that revision.
2. Run shared Redis for every API and ingestion-worker replica. Connector locking, provider budgets, queue state, and event deduplication rely on shared Redis state.
3. Configure `OTEL_EXPORTER_OTLP_ENDPOINT` for the API and both worker processes. Use the distinct service names `ekip-api`, `ekip-ingestion-worker`, and `ekip-agents-worker` in dashboards.
4. Start with `INGESTION_WORKER_MAX_JOBS=2`, `EMBEDDING_WORKER_THREADS=1`, and `EMBEDDING_BATCH_SIZE=32`. Increase only after observing CPU, resident memory, database connections, and queue age during a representative full sync.

## Operational signals

Poll these while a sync is running:

- `GET /tenancy/connectors/{connector_id}/runs` for per-run page/item/chunk progress and failure stage.
- `GET /observability/ingestion` for success, failure, dead-letter, retry, and throughput totals.
- `GET /observability/ingestion/queue` for backlog depth and oldest queued age.
- OTLP spans `ingestion.fetch_page`, `ingestion.process_item`, and `retrieval.embed` to separate provider latency, processing latency, and model latency.

Recommended paging conditions are a dead-letter count above zero, oldest queue age above five minutes for ten minutes, or failure ratio above 10% over fifteen minutes. Tune these after a week of baseline traffic.

## Dead-letter replay

1. Inspect the run's `failed_stage`, `last_error_type`, completed progress, and corresponding trace/logs.
2. Correct credentials, provider availability, resource limits, or malformed source data first.
3. Replay only the exhausted run with `POST /tenancy/connectors/{connector_id}/runs/{job_id}/replay`.
4. Confirm a new run appears. The old dead-letter row is immutable history; replay never rewrites it.

## Webhook adapters

Provider-facing adapters must verify the provider signature and delivery timestamp before calling `POST /tenancy/connectors/{connector_id}/events` with an application identity. Map the provider delivery identifier to `event_id`. The API hashes it into a deterministic ARQ job id, so redelivery is accepted but does not enqueue duplicate work. Hourly reconciliation remains enabled to recover missed events.

## Load baseline

Run in staging with a production-like database, Redis, worker size, and connector data containing both small and maximum-expected documents:

1. Full sync one connector and record documents/minute, chunks/minute, p50/p95 provider fetch duration, p50/p95 embed duration, peak RSS, CPU, database connections, and WAL/storage growth.
2. Enqueue `2 * worker_max_concurrency`, then `5 * worker_max_concurrency` different connectors. Confirm the queue drains, oldest age returns toward zero, and provider rate never exceeds the configured organization limit.
3. Redeliver the same event id 100 times. Confirm only one ARQ job is created.
4. Re-run unchanged data. Confirm `items_skipped` rises while `chunks_embedded` remains near zero.
5. Exercise documents just below and above the byte/chunk limits. Confirm the former succeeds and the latter fails clearly without worker memory growth.

Release gate: no stuck `running` jobs, no duplicate concurrent run for one connector, no dead letters, p95 queue age below the agreed SLO, and peak memory below 70% of the worker limit.

## Failure drills

Perform these only in staging or an approved maintenance window:

| Drill | Action | Expected result |
|---|---|---|
| Provider timeout | Blackhole the provider host beyond its HTTP timeout | bounded fetch retries; retry resumes the last completed page |
| Provider throttle | Return HTTP 429 with `Retry-After` | delay is honored up to 60 seconds; permanent 4xx is not retried |
| Worker death | terminate a worker after a completed page | connector lock expires safely; retry resumes the stored cursor |
| Redis interruption | stop Redis briefly | no split-brain connector execution; retry/readiness degradation is visible |
| Database disconnect | interrupt a connection mid-document | current item retries; completed items stay durable |
| Cyclic pagination | make an adapter repeat its next cursor | safety error, bounded retries, then dead letter |
| Embedding stall | delay model encode beyond 120 seconds | encode times out without blocking the event loop indefinitely |

After each drill, verify connector status, run history, queue depth, lock cleanup, checkpoint redaction, and trace continuity. Record measured recovery time and update the SLO rather than relying on estimates.
