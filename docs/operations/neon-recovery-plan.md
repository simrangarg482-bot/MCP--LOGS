# Neon recovery plan — ordered, not yet executed (Phase 4.7B Part 17/18)

**Status: prepared, NOT executed.** Nothing below has been run against the
real Neon database. Execution is gated on the disposable-PostgreSQL
validation in `docs/operations/migration-recovery.md`/Phase 4.7B actually
passing first, and on your explicit go-ahead — this is a plan, not an
authorization to proceed.

## Preconditions (must all be true before step 1 runs)

- [ ] `scripts/rls_isolation_test.py` passes against a real disposable
      PostgreSQL+pgvector instance (non-superuser role, pooled-connection
      reuse, concurrency, fail-closed with no context).
- [ ] A fresh `alembic upgrade head` on that same disposable instance reaches
      `d706a360fc2a` cleanly, followed by `alembic check` and
      `scripts/migration_status.py` both reporting OK.
- [ ] RBAC+RLS combination tests (Part 9) pass on that instance.
- [ ] You have explicitly confirmed you want the ordered steps below run
      against the real Neon database.

## Ordered recovery steps

```
1. Backup verification
   - Confirm the existing pg_dump backup from Batch 4.6
     (neon_ekip_pre_recovery.dump, neon_ekip_pre_recovery_schema_only.sql)
     is still present and its timestamp still predates any planned change.
   - If more than a few days have passed, or any data has changed since,
     take a fresh pg_dump before proceeding -- a stale backup is not a
     current one.

2. Migration inspection (read-only, repeat immediately before executing)
   - uv run python scripts/migration_status.py
     Expected today: UNRESOLVED (b3d8f1a6c9e2 not in repo history) -- this
     is the expected starting state this whole plan exists to fix.
   - Re-run the read-only schema snapshot approach from
     docs/operations/neon-schema-snapshot-2026-08-18.md to confirm nothing
     about the live database has changed since that snapshot was taken.

3. Orphaned object cleanup (physical DDL, via 90ff736ced55's upgrade())
   - This is what `alembic upgrade` would run once Neon's alembic_version
     is reconciled (step varies depending on whether Neon is stamped first
     or the DDL is run first -- see the "Alembic vs. physical schema"
     ordering note below).
   - Drops ONLY: eval_case_results, eval_runs (+ their RLS policies), and
     agent_executions.{model_used, prompt_tokens, completion_tokens,
     total_tokens}.
   - Deliberately does NOT touch: ekip_app role, incident:read/
     postmortem:read permission rows, or resolve_user_first_organization()
     -- see docs/operations/migration-recovery.md's security section for
     why these three are NOT "orphaned," they're inert progress on two
     still-open security recommendations.

4. Apply 90ff736ced55
   - Requires Neon's alembic_version to first be reconciled to a revision
     Alembic recognizes -- it currently is not (b3d8f1a6c9e2). The
     practical sequence:
     a. Manually verify (via the read-only introspection from step 2) that
        the physical schema, once step 3's DDL runs, will match exactly
        what e2b3c4d5f6a7 + 90ff736ced55's upgrade() together define.
     b. Run 90ff736ced55's upgrade() DDL directly (not via `alembic
        upgrade`, which can't run from an unrecognized current revision).
     c. Stamp alembic_version to '90ff736ced55' directly
        (`UPDATE alembic_version SET version_num = '90ff736ced55'`) --
        metadata-only, no further schema change, only after (a) and (b)
        are both confirmed.

5. Apply d706a360fc2a
   - Now that alembic_version correctly reads '90ff736ced55', this one CAN
     run through the normal `alembic upgrade head` path (or one revision:
     `alembic upgrade d706a360fc2a`) -- no manual stamping needed, since
     Alembic's own bookkeeping is consistent again as of step 4c.
   - Confirms: `incident:read` permission row exists, backfilled to every
     role that existed at this point.

6. RLS integration migration (if the ekip_app role-switch is also approved)
   - NOT bundled into steps 3-5 above -- a separate, later decision. If/when
     approved, this would be its own new migration on top of d706a360fc2a
     that formally provisions `ekip_app` (or an equivalent role) via a
     real, git-tracked migration (matching f4a7c2e9b3d1's definition,
     re-authored fresh, not resurrected verbatim) plus a corresponding code
     change to `resolve_organization_for_login` to call
     `resolve_user_first_organization()` (or an equivalent) instead of the
     current plain, RLS-unsafe ORM query -- see "Neon role migration
     design" below and migration-recovery.md's finding #3 for why both are
     needed together.

7. Alembic verification
   - alembic current   -> expect d706a360fc2a
   - alembic heads      -> expect d706a360fc2a (single head)
   - alembic check      -> expect no drift
   - uv run python scripts/migration_status.py -> expect "OK -- database is
     at the repository's current head"

8. Role verification (only relevant once/if step 6 is approved and applied)
   - Confirm ekip_app: super=false, bypassrls=false, login=true
   - Confirm the actual DATABASE_URL used by the application/worker/CI has
     been switched (see "Neon role migration design" below) -- verifying
     the role exists is not the same as verifying anything connects as it.

9. Application verification
   - Full local test suite (backend, import-linter, frontend) against the
     repaired database's connection string.
   - Manually or via `scripts/seed_test_organization.py`: exercise login,
     incident CRUD, postmortem generation, knowledge review, audit log,
     connector registration -- the modules whose RLS policies would newly
     be enforced for real if step 6 also lands.

10. Tenant-isolation verification
    - Two real organizations, real data, confirm cross-organization
      requests are denied end-to-end (not just at the service-layer
      permission check, but that RLS itself also holds if step 6 landed).
    - This step is the Neon-specific analogue of
      `scripts/rls_isolation_test.py` -- same assertions, real production-
      adjacent data instead of disposable throwaway rows.
```

## Alembic-vs-physical-schema ordering note

Steps 3-4 above look backwards at first glance (run DDL, THEN stamp) but
this is deliberate: Alembic itself refuses to run `upgrade` from a revision
it doesn't recognize (`b3d8f1a6c9e2`), so there is no way to have Alembic
"discover" the DDL for us here the normal way. The physical schema change
happens first (via the migration file's own `upgrade()` function, executed
directly against a confirmed-matching starting state), and only once that's
verified to match reality exactly does the metadata stamp follow — this is
the "evidence-backed stamp, not a guess" principle from
`docs/operations/migration-recovery.md` applied concretely.

## Rollback if any step fails

See `docs/operations/rollback.md`'s general guidance, plus the specific
`downgrade()` in `90ff736ced55` (drops-then-recreates the four objects it
removes) if step 3/4 needs reversing. If something fails after step 4c's
stamp, the full `pg_dump` backup from step 1 is the fallback of last resort.

---

# Neon role migration design (`neondb_owner` → `ekip_app`)

**Design only — not applied.** This is the separate, larger decision step 6
above refers to: how the application would actually start connecting as a
non-superuser role, once that's approved. Distinct from the schema-recovery
steps above, which don't require this to land first.

## The core constraint

Alembic migrations need DDL privileges (`CREATE TABLE`, `ALTER TABLE`,
`CREATE POLICY`, `CREATE FUNCTION`, and — for `f4a7c2e9b3d1`-style
migrations — even `CREATE ROLE`) that a genuinely least-privilege runtime
role should never hold. **The migration identity and the runtime
application identity must be different roles, connecting via different
connection strings**, per this phase's own "do NOT make the runtime
application role the migration superuser" instruction.

## Two identities, four connection strings

| Identity | Role | Used by | Privileges |
|---|---|---|---|
| **Migration identity** | `neondb_owner` (unchanged) | `alembic upgrade`/`downgrade` — CI's `migration-validation` job, and any real deployment's one-shot migration step (`docker-compose.yml`'s `migrate` service, `scripts/deploy.sh`'s job) | Full DDL — table owner, as today |
| **Runtime application identity** | `ekip_app` (new) | The FastAPI backend, the arq worker — every process that serves live traffic or runs background jobs | `SELECT`/`INSERT`/`UPDATE`/`DELETE` on tables + `USAGE`/`SELECT` on sequences + `EXECUTE` on functions only (exactly `f4a7c2e9b3d1`'s existing grants) — `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, `NOCREATEROLE` |

Concretely, this means **two different `DATABASE_URL`-shaped values**, not
one:

- `DATABASE_URL` (or a new, explicitly-named `MIGRATION_DATABASE_URL`) —
  keeps using `neondb_owner`. Only ever read by the migration step, never
  by `app.database.session`'s engine that the running API/worker use.
- A second setting (e.g. `APPLICATION_DATABASE_URL`, or `DATABASE_URL`
  itself repointed to `ekip_app` once migrations are decoupled from normal
  app startup — which they already are, per section 22's "explicit
  deploy → migration → application" ordering this codebase already
  follows) — used by `app/database/session.py`'s `_build_engine()`, the arq
  worker's own session construction, and `app.mcp`'s session handling. All
  three currently derive from the same `Settings.database_url` — this
  would need to become the `ekip_app`-connecting value.
- **Test connection**: CI's disposable Postgres service containers already
  use their own throwaway superuser-equivalent role (`ekip`/`ekip_ci_only`
  in `docker-compose.yml`/the GitHub Actions workflows) — no change needed
  there; those are disposable databases, not a security boundary to harden.
  `scripts/rls_isolation_test.py` deliberately creates and uses its own
  dedicated non-superuser role for exactly this reason, independent of
  whatever `DATABASE_URL` points at.
- **Worker connection**: currently derived from the same `Settings.
  database_url` as the API (`app.ingestion.workers.main`,
  `app.agents.workers.tasks` both go through `app.database.session`) — no
  separate worker-specific credential exists today, and none is proposed
  here; the worker becomes `ekip_app` at the same time the API does, via
  the same setting.

## Required code changes (not yet made)

1. `app/shared/config/settings.py` — add a distinct setting for the
   migration connection string if `alembic`'s own `env.py` should keep
   using the owner role while `Settings.database_url` moves to `ekip_app`
   (today, `app/database/migrations/base.py` reads the same
   `Settings.database_url` alembic and the app both use — this coupling
   would need to be broken first).
2. `app/core/users/service.py::resolve_organization_for_login` /
   `app/core/users/repository.py::get_first_organization_id` — must call
   the `resolve_user_first_organization()` SECURITY DEFINER function
   (mirroring `resolve_refresh_token_organization`'s existing pattern in
   `core.auth.service`) instead of the current plain ORM query, which would
   silently return `NULL` for every password login under real RLS
   enforcement (no tenant context exists yet at this exact point) — see
   `docs/operations/migration-recovery.md`'s finding #3 for the full
   reasoning. **This is not optional if `ekip_app` goes live** — password
   login breaks without it.
3. A new migration (step 6 above) that creates `ekip_app` (or re-uses it,
   already present in Neon) AND `resolve_user_first_organization()` on
   `main`'s own tracked history — both currently exist only as live,
   uncommitted database objects.

## Verification required before switching (not yet done)

Every RLS-protected code path re-exercised under the new role specifically
— a role having the right `GRANT`s is necessary but not sufficient; RLS
policies must also be confirmed correct for every access pattern the
application actually performs (see Part 8/9 of this batch). This is exactly
what `scripts/rls_isolation_test.py` plus a full manual walkthrough (login,
incident CRUD, postmortem generation, knowledge review, audit log, connector
registration, ingestion) against a disposable database must prove before
this design is applied anywhere real.
