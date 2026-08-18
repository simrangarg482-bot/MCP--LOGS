# Migration recovery — Neon development database (2026-08-18)

## Status

```
MIGRATION STATE: UNRESOLVED (still — see "Current status", below)
DECISION:        Option 3 (restore to main's schema), PARTIALLY EXECUTED
BACKUP:          TAKEN — see "Backup" section
```

No schema-modifying command has been run against the real Neon database as
of this writing. A recovery migration has been drafted, reviewed, and is
ready to apply (`app/database/migrations/versions/
90ff736ced55_batch_4_6_remove_orphaned_simran_ekip_.py`) — but it covers only
part of the originally-scoped "remove all orphaned branch-only state"
decision. Two of the six identified extra objects turned out, on closer
investigation, to not be dead scaffolding at all — see "Why Option 3 is only
partially applied" below before running anything.

## What triggered this

```
$ uv run python scripts/migration_status.py
Database revision:     'b3d8f1a6c9e2'
Repository head(s):    e2b3c4d5f6a7
Revision exists?       False
Schema compatibility:  UNRESOLVED
```

`b3d8f1a6c9e2` does not appear in `app/database/migrations/versions/` on
`main`, on `origin/simran-ekip`, or anywhere in `git log --all` (searched by
both filename and file content across every commit on every branch/ref).

## What the database actually contains (read-only introspection)

| Database object | Repository (`main`) expects | Database contains | Difference |
|---|---|---|---|
| `alembic_version` | one of the 10 known revision ids, ideally `e2b3c4d5f6a7` (head) | `b3d8f1a6c9e2` | **Unknown revision id — not in any tracked migration file** |
| `oauth_clients` table | present (`b4c7e2a9f5d1`, main-only branch) | present | Match |
| `code_chunks.repo_full_name` | present (`d1a2b3c4e5f6`, main-only) | present | Match |
| `documentation_chunks.repo_full_name` | present (`e2b3c4d5f6a7`, main head) | present | Match |
| `mcp_requests` table | present (`e3f6a1b8d4c9`, common ancestor of both branches) | present | Match |
| `eval_runs`, `eval_case_results` tables + `tenant_isolation` RLS policies | **not present** — no migration on `main` creates them | present | **Extra: exists only on `origin/simran-ekip`'s `d4f7b2e9c6a3`** |
| `agent_executions.model_used` / `.prompt_tokens` / `.completion_tokens` / `.total_tokens` | **not present** on `main` | present | **Extra: exists only on `origin/simran-ekip`'s `d8a2f6c1b9e3`** |
| `ekip_app` Postgres role | **not present** — no migration on `main` creates it | present | **Extra: exists only on `origin/simran-ekip`'s `f4a7c2e9b3d1`** |
| `permissions` table rows | 7 codes (`scripts/seed_test_organization.py`'s `_ALL_PERMISSION_CODES`) | 9 codes — also has `incident:read`, `postmortem:read` | **Extra: seeded only by `origin/simran-ekip`'s `b6e9c2a4f7d1`** |
| `resolve_user_first_organization()` function | **not present** — no migration on `main` or `origin/simran-ekip` creates it | present, `GRANT EXECUTE`-ed to `ekip_app` by name | **Extra: exists in NO git history at all — created directly against the live database, correction below** |
| Total tables | 31, if both chains' additive migrations are combined | 31 | Match (see below) |

**The database is not in some unknown, unexplainable state.** Every object
in it is accounted for by exactly one of two migration chains that share a
common ancestor and then diverge:

```
                              e3f6a1b8d4c9  (common ancestor — both chains agree here)
                             /            \
   main chain:     b4c7e2a9f5d1            f4a7c2e9b3d1   :simran-ekip chain
                    │                            │
                   c8f1a4d7e2b3                 b6e9c2a4f7d1
                    │                            │
                   d1a2b3c4e5f6                 d4f7b2e9c6a3
                    │                            │
                   e2b3c4d5f6a7 (main HEAD)      d8a2f6c1b9e3 (simran-ekip HEAD)
```

The Neon database has **both** branches' changes applied — every table/
column/role identified in the original investigation is explained by one of
the two chains. **Correction (Batch 4.6):** one further object was found
during the follow-up schema snapshot that neither chain explains:

```sql
CREATE OR REPLACE FUNCTION public.resolve_user_first_organization(user_id_arg uuid)
 RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path TO 'public'
AS $function$ SELECT organization_id FROM user_roles WHERE user_id = user_id_arg LIMIT 1; $function$
```

Confirmed via `git log --all -S` (both filename and content, across every
branch) to exist in **no commit anywhere** — not `main`, not
`origin/simran-ekip`. It follows the exact `SECURITY DEFINER` +
`SET search_path = public` narrow-bypass pattern `main`'s own
`d2e5f8a3c1b6` migration established (a pre-Identity "which org does this
belong to" lookup, the same shape as that migration's four functions), and
is explicitly `GRANT EXECUTE`-ed to `ekip_app` by name (not just `PUBLIC`) —
strong evidence someone was actively prototyping a login/auth flow against
the `ekip_app` role specifically, i.e. **further along on the same open
"non-superuser application role" security recommendation** than either
committed branch shows, created directly against the live database and
never captured in any migration at all. Treated exactly like `ekip_app`
itself: not dropped, not guessed about further, called out here so it isn't
silently lost or silently declared "fully explained" when it isn't yet. A
full audit for any *other* uncommitted, directly-created objects has not
been performed — this one was found only because the follow-up snapshot
happened to enumerate `SECURITY DEFINER` functions explicitly.

## Diagnosis

`b3d8f1a6c9e2` was almost certainly a **merge revision** — the file Alembic
generates via `alembic merge -m "..." e2b3c4d5f6a7 d8a2f6c1b9e3`, which joins
two independent heads into one so `alembic upgrade head` has a single target
again. Someone applied it to this database (`alembic upgrade head` picks up
a merge revision automatically once one exists), but the generated file was
never committed — this repo's own `git reflog` shows several hard resets
(`reset: moving to HEAD`, `reset: moving to origin/main`) in the same period,
any of which would silently discard an uncommitted local file.

This is a diagnosis backed by direct schema comparison, not a guess from
timestamps — every single object found is explained by one of the two real,
existing chains; there is no unaccounted-for table, column, or role.

## Why this was NOT auto-repaired

Per the explicit instruction for this work: **never stamp a database
migration revision based on a guess.** A merge revision's `upgrade()`
function *could* contain more than a no-op merge marker (Alembic allows
arbitrary code in a merge migration, same as any other) — since the actual
file was never seen, its exact content cannot be confirmed, only inferred
from what the database now contains versus what both parent chains'
*existing, reviewable* files already account for. Stamping to any existing
single revision (e.g. `e2b3c4d5f6a7`) would make Alembic falsely believe the
`origin/simran-ekip`-only objects don't exist — a future `alembic upgrade
head` run from `main` wouldn't recreate them (they're additive, so nothing
would break immediately), but any future merge of `origin/simran-ekip` into
`main` would then hit "table already exists" / "role already exists"
errors, because Alembic would have no record that this database has already
seen `f4a7c2e9b3d1` through `d8a2f6c1b9e3`.

## Decision: Option 3 (2026-08-18)

The database is restored to `main`'s own schema, not merged with
`origin/simran-ekip`'s. The orphaned branch-only objects are removed —
**with two confirmed exceptions**, below — via a new, reviewed, git-tracked
migration on `main`'s own chain, never a stamp pretending the schema already
matched `e2b3c4d5f6a7`.

## Backup (Part B — done before any DDL)

Taken via `pg_dump` against the live Neon database, using this machine's
local PostgreSQL 18 client tools (no Neon console access available from this
environment):

- **Custom-format full dump** (schema + data, restorable with `pg_restore`,
  `--no-owner --no-privileges` so restore doesn't fight over role
  ownership): `neon_ekip_pre_recovery.dump`, 638,551 bytes.
- **Schema-only plain-SQL dump** (for human review/diffing):
  `neon_ekip_pre_recovery_schema_only.sql`, 67,279 bytes.
- **Timestamp**: 2026-08-18, immediately before any DDL was drafted or run —
  recorded here rather than in a file under `docs/` per Part C's "no
  sensitive information" rule (a full dump is exactly the kind of file that
  must never be committed to this repository).
- **Location**: local scratch directory, not committed, not under `docs/` —
  a full database dump is sensitive by nature (contains real row data:
  users, incidents, connector configs) regardless of whether it contains
  literal credentials.
- **Restore mechanism**: `pg_restore --no-owner --no-privileges -d
  <target-dsn> neon_ekip_pre_recovery.dump` against a fresh or the same
  database. Not yet test-restored to a second database in this pass — doing
  so would itself require a second disposable Postgres instance, which this
  environment doesn't have (see "Known limitations" in
  `docs/operations/local-production.md`). Treat the dump as unverified-by-
  restore until that gap is closed.
- **Database owner**: connects as `neondb_owner` (Neon's default owner
  role for this project) — see the RLS-bypass finding below for why this
  same fact is also a live security gap, not just an ownership note.

## Schema snapshot (Part C)

A full metadata-only snapshot (tables, columns, indexes, constraints,
extensions, enum types, RLS policies, `SECURITY DEFINER` functions, and role
attributes — no row data, no credentials) was captured via read-only
`pg_catalog`/`information_schema` queries immediately after the backup. It
lives alongside the backup files in the same local scratch location, not
under `docs/`, purely because it's large and situational rather than because
it contains anything sensitive — the object-by-object findings from it are
already reproduced in the table above and the sections below.

## Why Option 3 is only partially applied

Two of the six originally-identified "branch-only" objects are **not**
orphaned scaffolding — closer investigation (re-checking, as required,
whether current `main` code actually depends on each object before removing
it) found they are the inert remnant of an **unmerged fix for a real,
currently-live security gap**, not dead feature-branch code:

1. **The `ekip_app` Postgres role** (`NOSUPERUSER`, `NOBYPASSRLS`,
   confirmed via direct `pg_roles` introspection: `super=false
   bypassrls=false login=true`) is exactly the non-superuser application
   role `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md`'s recommendation #2
   calls "still open, and now the top priority." That same introspection
   confirmed `neondb_owner` — the role `DATABASE_URL` actually connects
   as today — has `bypassrls=true`. **Every RLS policy in this database is
   currently a live no-op for the application's real connection.** Dropping
   the one already-correctly-provisioned role sitting in this database
   would not clean up dead code; it would erase real progress on an open,
   documented, now-confirmed-live security recommendation.
2. **The `incident:read` / `postmortem:read` permission catalog rows**
   (seeded by `b6e9c2a4f7d1`). That migration's own docstring documents a
   prior audit finding ("2026-08 audit 'H4'") that remains true on `main`
   today, confirmed by direct code reading: `app.core.incidents.service.
   get_incident`, `list_incidents`, and `get_timeline` — all backed by
   `_get_owned_incident`, which checks only `organization_id` equality —
   have **no permission check at all**, unlike every write path in the same
   module. Any identity in the organization, including one with zero role
   assignments, can read every incident's full detail and timeline across
   every project. The corresponding code fix
   (`require_project_permission(actor, project_id, "incident:read")` on
   those three read paths) exists only on `origin/simran-ekip`, never
   merged. Dropping these two catalog rows fixes nothing (the vulnerability
   is the missing code-level check, not the absence of these rows) and only
   makes the eventual real fix redo the seed-and-backfill-to-every-role work
   `b6e9c2a4f7d1` already did correctly. **This finding is now also
   recorded in `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md`'s own
   recommendations list (items 2 and 3)** — that document, not this one, is
   the authoritative tracker for closing it; this file exists to explain why
   the migration recovery below deliberately leaves these two objects alone.

The remaining four objects — `eval_runs`, `eval_case_results` (+ their
`tenant_isolation` RLS policies), and the four `agent_executions`
model-routing columns — were confirmed to have no such hidden justification:
`EKIP_STRATEGIC_ANALYSIS.md` describes both as **proposed future work**
("Priority: Highest" for the eval harness; "the table already exists, just
add columns" for model routing, describing exactly this branch's premature
state), and `scripts/eval_confidence.py` runs today without touching either
table at all. These four are dropped by the recovery migration below.

## Recovery migration (Part E)

`app/database/migrations/versions/
90ff736ced55_batch_4_6_remove_orphaned_simran_ekip_.py`, `down_revision =
e2b3c4d5f6a7` (main's real head) — **drafted, reviewed, syntax-validated,
NOT YET applied to Neon.**

```
upgrade():
  DROP POLICY/disable RLS on eval_runs, eval_case_results
  DROP TABLE eval_case_results (child — FK to eval_runs)
  DROP TABLE eval_runs (parent)
  DROP COLUMN agent_executions.{model_used, prompt_tokens, completion_tokens, total_tokens}

downgrade():
  Recreates exactly what d8a2f6c1b9e3/d4f7b2e9c6a3 originally defined —
  this migration's own reversal, not a resurrection of the lost/
  never-committed b3d8f1a6c9e2 merge revision.
```

Deliberately does **not** touch `ekip_app` or the `incident:read`/
`postmortem:read` permission rows — see above.

Verified so far: migration file loads cleanly (`ScriptDirectory.
walk_revisions()` succeeds), produces a single new head (`90ff736ced55`),
and the full backend test suite (418 passed) + import-linter (7/7) still
pass with it present. **Not yet applied to any database, disposable or
Neon** — see "Fresh database proof" below for why, and "Current status" for
what's still needed before it can run.

## Why `b3d8f1a6c9e2` is not recreated (Part E.4)

The lost merge revision's exact `upgrade()` contents were never seen —
only its effect (the union of both chains' schema) was ever observed.
Recreating a migration file claiming to *be* `b3d8f1a6c9e2` would be
exactly the kind of guess this whole investigation exists to avoid: Alembic
merge migrations can contain arbitrary code beyond a bare merge marker, and
there is no way to confirm this one didn't. `90ff736ced55` is deliberately a
**new, independent revision** — the physical database will end up at a real,
newly-authored, git-tracked head that was actually reviewed, not a
resurrection of an unverifiable historical one.

## Fresh database proof (Part F)

**Not yet completed.** This environment has no Docker daemon and the one
local PostgreSQL instance available has no `pgvector` extension installed,
so a genuine "empty Postgres → `alembic upgrade head` → compare against the
recovered Neon schema" run isn't possible here. `alembic check` and
`scripts/migration_status.py` have been added as permanent steps in
`.github/workflows/main-extra.yml`'s `migration-validation` job (against a
disposable `pgvector/pgvector:pg16` service container) — the first real run
of that job in GitHub Actions is what will actually discharge this
requirement; it has not run yet (no `.github/workflows/` history in this
repository until this batch).

**Related, also blocked by the same environment gap**:
`scripts/rls_isolation_test.py` (Phase 4.7.9/4.7.10) — a real, disposable-
database proof that RLS/tenant-context isolation actually holds under a
non-superuser connection role and survives pooled-connection reuse. Written
and syntax-checked, not yet executed — see that script's own module
docstring for exactly what it proves and why it must never be pointed at
this Neon database (whose connection role bypasses RLS entirely, which
would make every assertion in it pass trivially regardless of whether the
underlying mechanism works).

## Current status

```
MIGRATION STATE:     UNRESOLVED — Neon's alembic_version is still b3d8f1a6c9e2;
                      nothing has been written to that database yet.
Recovery migration:  DRAFTED, reviewed, not applied.
Next action needed:  explicit confirmation before this migration is applied
                      to the shared Neon database, and a decision on how (or
                      whether, and on what timeline) to fix the two
                      confirmed-live security gaps above.
```

Once the drop migration is applied and Alembic's stamp is set to
`90ff736ced55` (only after physically confirming the schema matches, exactly
as this migration defines — not before), `alembic current`, `alembic
heads`, and `scripts/migration_status.py` should all agree, followed by the
full local verification suite re-run against this database specifically.

## Rollback strategy for this recovery itself

If `90ff736ced55` is applied and something unexpected breaks: `alembic
downgrade -1` recreates `eval_runs`/`eval_case_results`/the four
`agent_executions` columns exactly as written above (a real, working
downgrade, not a stub) — safe because nothing on `main` ever wrote to those
objects, so there's no data-loss risk in dropping-then-recreating them. If
that's ever insufficient, `pg_restore` from the `neon_ekip_pre_recovery.dump`
backup taken above is the full fallback, per `docs/operations/rollback.md`'s
general "take a backup immediately before any production/shared-database
migration" guidance.

## What was verified as NOT affected

`main`'s own application code has zero dependency on `eval_runs`/
`eval_case_results`/the four `agent_executions` columns — confirmed by
direct grep across `app/`, `scripts/`, `tests/`, and `frontend/`, not
inferred. This is not a live model/schema drift bug in `main` for those four
objects. The other two (`ekip_app`, `incident:read`/`postmortem:read`) are a
different story — see above; they relate to `main` by way of an open,
unfixed security gap, not a code dependency. CI's own migration validation
(`.github/workflows/main-extra.yml`) runs against a disposable, freshly
created Postgres service container — entirely independent of this Neon
database — so it is, and remains, unaffected by any of the above.
