# Neon development database — schema snapshot (2026-08-18)

Point-in-time, metadata-only snapshot taken immediately before the Batch 4.6
migration recovery work (`docs/operations/migration-recovery.md`), via
read-only `pg_catalog`/`information_schema` queries. No row data, no
credentials — safe to keep in version control. A full `pg_dump` backup
(schema + data) was also taken but is **not** committed here (see
`migration-recovery.md`'s "Backup" section for why — it contains real row
data, unlike this file).

This is a frozen record of what the database looked like on this date, not
a living reference — `scripts/migration_status.py` is the tool to check
current state going forward.

## `alembic_version` at time of snapshot

```
b3d8f1a6c9e2   (see migration-recovery.md — not a real revision in any branch's history)
```

## Extensions

```
plpgsql 1.0
vector  0.8.1
```

## Tables (31)

```
agent_executions, alembic_version, audit_logs, code_chunks, connector_configs,
conversations_chunks, document_metadata, documentation_chunks, documents,
eval_case_results, eval_runs, external_identity_mappings, incident_timeline,
incidents, ingestion_jobs, invitations, knowledge_gap_reports, mcp_requests,
oauth_clients, organization_access_rules, organizations, permissions,
postmortems, project_memberships, projects, refresh_tokens, role_permissions,
roles, sso_configurations, user_roles, users
```

`eval_runs`/`eval_case_results` and `agent_executions.{model_used,
prompt_tokens, completion_tokens, total_tokens}` are the objects
`90ff736ced55` (the Batch 4.6 recovery migration) removes. Every other table/
column here matches `main`'s own migration chain through `e2b3c4d5f6a7`.

## RLS-enabled tables (23, all `FORCE ROW LEVEL SECURITY`)

```
agent_executions, audit_logs, code_chunks, connector_configs,
conversations_chunks, document_metadata, documentation_chunks, documents,
eval_case_results, eval_runs, external_identity_mappings, incident_timeline,
incidents, ingestion_jobs, invitations, knowledge_gap_reports,
organization_access_rules, postmortems, project_memberships, projects,
refresh_tokens, sso_configurations, user_roles
```

All using the single `tenant_isolation` policy from `c7d4e8f19a2b`, one of
two shapes:

```sql
-- direct organization_id column (most tables):
organization_id = current_setting('app.current_organization_id', true)::uuid

-- indirect, via a parent table (document_metadata, project_memberships):
document_id IN (SELECT id FROM documents WHERE organization_id = current_setting(...)::uuid)
project_id  IN (SELECT id FROM projects  WHERE organization_id = current_setting(...)::uuid)
```

**Not RLS-protected by design**: `permissions`, `roles`, `role_permissions`
(global catalogs, no `organization_id`), `mcp_requests`, `oauth_clients`
(same reasoning per their own migrations' docstrings), `alembic_version`.

## `SECURITY DEFINER` functions (narrow RLS-bypass helpers)

```
resolve_connector_config_organization(uuid)   -- main, d2e5f8a3c1b6
resolve_document_organization(uuid)           -- main, d2e5f8a3c1b6
resolve_refresh_token_organization(text)      -- main, d2e5f8a3c1b6
list_active_connector_config_ids()            -- main, d2e5f8a3c1b6
resolve_user_first_organization(uuid)         -- ⚠ NOT IN ANY GIT HISTORY —
                                                  see migration-recovery.md's
                                                  correction section
```

Everything else in the database's function list is pgvector's own
non-`SECURITY DEFINER` operator/type-support functions (`vector_*`,
`halfvec_*`, `sparsevec_*`, distance operators) — not application code.

## Non-default roles

```
role            super  bypassrls  login
─────────────── ────── ────────── ─────
cloud_admin     true   true       true    (Neon-managed, not application-relevant)
neon_superuser  false  true       false   (Neon-managed)
neon_service    false  true       true    (Neon-managed)
neon_auth       false  false      true    (Neon-managed)
neondb_owner    false  true       true    ⚠ what DATABASE_URL connects as today
ekip_app        false  false      true    the correctly-provisioned role, unused by main's config
```

`neondb_owner` having `bypassrls=true` is the confirmed-live finding behind
`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #2 — every RLS
policy above is currently a no-op against the application's real connection.

## Permission catalog (9 codes)

```
audit:read, incident:read*, incident:write, knowledge:review,
observability:read, postmortem:approve, postmortem:read*, postmortem:write,
tenancy:manage
```

`*` = seeded only by the abandoned branch (`b6e9c2a4f7d1`), corresponding to
a currently-unfixed `main` vulnerability — see
`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #3. The other 7
match `scripts/seed_test_organization.py`'s `_ALL_PERMISSION_CODES`.
