"""Scheduled retention of temporary and operational records.

Owned by: core/retention. Nothing here is user-facing: it is the logic behind the daily maintenance job the
ingestion worker runs (`app.ingestion.workers.tasks.scheduled_maintenance`). See `service.run_retention`.

What is deleted automatically, and ONLY after an explicit, configurable retention period
(`Settings.*_retention_days`):

* `mcp_requests` older than `MCP_REQUEST_RETENTION_DAYS` (all tenants, via the batched definer function);
* finished `ingestion_jobs` older than `INGESTION_JOB_RETENTION_DAYS` (never queued/running jobs, never a
  connector's most recent job);
* expired refresh tokens (after a grace period) and revoked refresh tokens (after a retention period);
* expired / consumed `email_verifications` and `password_resets`;
* expired / revoked SCIM tokens and decided held SCIM deactivations.

Active tokens, pending links and anything not yet past its period are never selected: each predicate below is the
exact definition, and each has a test.
"""
