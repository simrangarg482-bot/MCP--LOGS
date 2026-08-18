# `SECURITY DEFINER` function audit (Phase 4.7B Part 16)

Every `SECURITY DEFINER` function found in the live Neon database (via the
read-only schema snapshot, `docs/operations/neon-schema-snapshot-2026-08-18.md`).
All five exist for the identical reason: a narrow, pre-`Identity` "chicken-
and-egg" lookup that must run *before* `set_tenant_context` can be called
(the row being looked up is itself hidden by the RLS policy that same call
would otherwise satisfy) — the pattern `d2e5f8a3c1b6`'s own module docstring
establishes and names explicitly.

| Function | Source | Owner | `search_path` | Grants | Input validation | RLS interaction | Privilege-escalation risk |
|---|---|---|---|---|---|---|---|
| `resolve_connector_config_organization(uuid)` | `main`, `d2e5f8a3c1b6` | `neondb_owner` (the migration-running role) | `SET search_path = public` — pinned | Not independently re-queried per-function this pass; Postgres grants `EXECUTE` to `PUBLIC` by default on function creation unless explicitly revoked, and none of these migrations revoke it | Single `uuid` parameter, used only in a parameterized `WHERE id = $1` — no dynamic SQL, no injection surface | Returns exactly one column (`organization_id`) for one row — by design, the minimum needed to then call `set_tenant_context` and re-query normally | **Low** — narrow single-column SELECT, no mutation, no `SELECT *` |
| `resolve_document_organization(uuid)` | `main`, `d2e5f8a3c1b6` | Same | Same | Same | Same shape, `documents` table | Same | **Low** |
| `resolve_refresh_token_organization(text)` | `main`, `d2e5f8a3c1b6` | Same | Same | Same | Single `text` parameter (token hash), parameterized | Same, `refresh_tokens` table | **Low** |
| `list_active_connector_config_ids()` | `main`, `d2e5f8a3c1b6` | Same | Same | Same | No parameters | Returns only `id`s filtered to `status IN ('active','error')` — used solely by the scheduled cross-tenant reconciliation scan, which is deliberately cross-tenant by design (documented in the same migration) | **Low** |
| `resolve_user_first_organization(uuid)` | **Not in any git history — created directly against the live database** | `neondb_owner` (confirmed via direct `pg_proc`/`proowner` query) | `SET search_path TO 'public'` — pinned, same hardening | Confirmed via `information_schema.routine_privileges`: `EXECUTE` granted to `PUBLIC`, `neondb_owner`, and **`ekip_app` by name** | Single `uuid` parameter, parameterized `WHERE user_id = $1 LIMIT 1` | Identical shape/purpose to the four functions above, for a fifth chicken-and-egg case `d2e5f8a3c1b6`'s own enumeration missed: `core.auth.service.login_with_password` → `resolve_organization_for_login` → `repository.get_first_organization_id` runs *before* any tenant context exists, via a plain (currently RLS-bypassing-only-because-neondb_owner-bypasses-RLS-anyway) ORM query against `user_roles` | **Low**, same reasoning as the other four — narrow single-column SELECT |

## Verdict on `resolve_user_first_organization`

**Required, not insecure, not unused-in-spirit** — see
`docs/operations/migration-recovery.md`'s finding #3 for the full trace:
`core.auth.service.login_with_password` (line 399,
`resolve_organization_for_login`) calls a plain ORM query
(`repository.get_first_organization_id`) against the RLS-protected
`user_roles` table, with no RLS-bypass mechanism, at a point before any
tenant context can exist. This works today only because `neondb_owner`
bypasses RLS entirely, masking the gap. The moment the application actually
connects as a non-superuser role (`ekip_app`), password login would start
returning `NULL` for every account, since the fail-closed RLS policy hides
every `user_roles` row from a connection with no tenant context set yet.
`resolve_user_first_organization()` is exactly the fix — someone already
built it and (per its `ekip_app`-named grant) was actively testing it — but
never wired the application code to call it, and never committed a
migration for it.

**Action taken this phase**: documented in the Neon role migration design
(`docs/operations/neon-recovery-plan.md`) as a **required** companion to any
future `ekip_app` connection switch — not removed, not silently kept
undocumented. **Not brought into migration history yet** — that's step 6 of
the recovery plan, gated on the disposable-database validation this batch
was trying to run, and on your explicit approval of the broader role switch.

## Hardening opportunity (not a vulnerability, not applied)

All five functions rely on Postgres's default `EXECUTE ... TO PUBLIC` grant
rather than an explicit `REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO
ekip_app` pair. In this codebase's actual architecture this is low-risk in
practice — the only Postgres login roles that can ever open a connection at
all are the application's own service accounts (`ekip_app`, `neondb_owner`),
never individual end-users directly — but tightening it costs nothing and
follows least-privilege more precisely. Worth including in the same future
migration that formally brings `ekip_app` and this function into `main`'s
tracked history (step 6 of the recovery plan), not urgent enough to justify
a standalone migration on its own.
