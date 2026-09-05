# CI pipeline

Four GitHub Actions workflows, split by cost/speed tier (Phase 3 Batch 4,
plus the dedicated RLS security gate added afterward — see below):

## `.github/workflows/ci.yml` — every PR and every push to `main`

No service containers, no repository secrets. The entire backend test suite
mocks repository/DB calls end-to-end — verified directly by running it
against a deliberately unreachable `DATABASE_URL`/`REDIS_URL`, which still
passes all 417+ tests — so `Settings` only needs syntactically valid
placeholder values, never real connectivity.

- **secret-scan**: `gitleaks detect` against the full checkout, including git
  history (`fetch-depth: 0`) — not just the current diff, since a leaked
  credential is a problem the moment it's committed, whichever PR introduced
  it. Configured in `.gitleaks.toml` (extends gitleaks' default ruleset with
  one project-specific rule for connection strings with an inline
  `user:password@host`, matching the real incident that motivated adding
  this job — see `docs/operations/security-incidents.md`). Runs with
  `--redact`, so even a real match's CI output never contains the actual
  secret value, only its rule/file/line.
- **backend**: `uv sync --frozen --extra dev`, `pytest`, `lint-imports`, then
  the **deterministic AI evaluation regression gate** (see below).
- **frontend**: `npm ci`, `tsc --noEmit`, `eslint`, `vite build`.

This tier is intentionally the only one required to pass before merge —
everything else needs real infrastructure/secrets a contributor's PR
shouldn't be blocked on.

### Deterministic AI evaluation (in the `backend` job)

`uv run python scripts/run_evaluation.py --report-path run_evaluation_report.json`
runs `app/evaluation`'s Mode 1 harness — retrieval (Recall@K/Precision@K/MRR/
coverage), deterministic grounding + citation validation, answer assertions,
and investigation evidence/hypothesis checks — against the fixture corpus in
`app/evaluation/fixtures/`. It lives in *this* tier, not alongside
`eval_confidence.py` in `e2e-and-eval.yml`, specifically because it needs
none of what that tier needs: no live database, no `OPENAI_API_KEY`, no
service containers, no secrets. It is a step in the existing `backend` job
rather than its own job so it reuses that job's `uv sync` (a separate job
would rebuild the whole dependency tree for a ~10-second check).

**Exit-code semantics — the important part.** The gate is *not* "fail if any
evaluation case failed." The fixture suite deliberately ships negative
controls that are **supposed** to fail (10 of 28 cases as of `v1.1` of the
four datasets); they are what proves the evaluator detects real defects at
all. Gating on raw failures would make CI permanently red, and the obvious
way to "fix" that would be deleting exactly those controls. Instead each
dataset case declares `expected_outcome` (`"pass"` | `"fail"`) and,
optionally for expected failures, an `expected_failure_stage`
(`"retrieval"` | `"generation"`). The job fails only on a genuine
**regression**:

| Regression | Meaning |
|---|---|
| `unexpected_failure` | An `expected_outcome: "pass"` case failed — something that worked broke. |
| `unexpected_pass` | An `expected_outcome: "fail"` negative control passed — a check built to catch a specific defect has stopped catching it. Note this *raises* the raw pass count, which is why raw counts can't be the gate. |
| `wrong_failure_stage` | A control failed somewhere other than its pinned stage — still failing, no longer testing what it was written to test. |

A dataset validation error, a runner crash, or an unwritable report path also
fail the job: each means the gate never actually ran, which must never read
as success. All six of these conditions were verified locally by simulation
before this job was added.

The JSON report uploads as the `deterministic-evaluation-report` artifact
(30-day retention) on **every** run, not just failures — unlike the E2E
workflow's failure-only Playwright artifact. A green run's metrics are the
baseline that makes a slow quality drift (still passing, but Recall@5
sliding) visible at all. The report contains only fixture-corpus content and
computed metrics: no credentials, no customer data, nothing from any real
database.

## `.github/workflows/rls-security.yml` — every PR and every push to `main`

A dedicated, **mandatory** tenant-isolation security gate, independent of
both `ci.yml` (fully mocked — cannot validate real Postgres RLS at all) and
`e2e-and-eval.yml`'s OpenAI-gated tiers (which *skip*, not fail, when
`OPENAI_API_KEY` is unset — meaning tenant-isolation coverage must never
live only there, or a missing OpenAI key would leave the entire repository's
RLS story unverified while CI still shows green). This workflow needs no
external AI API, no embeddings, no LangGraph, and no production
credentials — only a disposable, CI-local `pgvector/pgvector:pg16` service
container, real Alembic migrations, and the real `ekip_app` runtime role
(`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #2).

Single job, **`rls-security-gate`**:

1. Starts the disposable Postgres + pgvector service container (its own,
   separate from `main-extra.yml`'s and `e2e-and-eval.yml`'s — never shared
   state between jobs or workflows).
2. `alembic upgrade head`, connected via `MIGRATION_DATABASE_URL` (the
   disposable container's own admin role, hardcoded CI-only, never a real
   secret) — runs every migration including `b8f3d6a1c4e7` (which
   provisions `ekip_app` itself) and `c7d4e8f19a2b`/`d2e5f8a3c1b6` (RLS
   policies + bypass functions).
3. `python scripts/verify_rls_isolation.py`, connected via `DATABASE_URL`
   (the same container's `ekip_app` role) — the actual security check: role
   identity, `rolbypassrls = false`, not superuser, required DML grants,
   RLS enabled *and* forced on every policy-bearing table, then the real
   cross-tenant proof (two organizations, one incident each, a query with
   **no** `organization_id` filter at all in either direction, plus a
   no-tenant-context fail-closed check). See that script's own docstring
   for the full contract, and its "KNOWN, OUT-OF-SCOPE FINDING" section for
   a real bug it works around rather than silently fixes
   (`tenancy_service.create_organization` — see the final report for this
   change).
4. Writes an explicit `RLS Security Gate: PASS`/`FAIL` line to the job
   summary (`if: always()`), so a failure is never confused with "didn't
   run."

Every `OPENAI_API_KEY`/`REDIS_URL`/`JWT_SECRET_KEY`/
`CONNECTOR_SECRET_MASTER_KEY` value in this workflow is a hardcoded,
CI-only placeholder, deliberately **not** sourced from `secrets.*` — this
job must behave identically whether or not `OPENAI_API_KEY` is configured
anywhere in the repository, and it never touches a real credential.

**This job should be configured as a required branch-protection status
check** (GitHub Settings → Branches → Branch protection rules → main →
"Require status checks to pass" → add `RLS Security Gate`). This repository
has no branch-protection config file and no way for a workflow file to
configure that setting itself — it needs a one-time manual change by a repo
admin with GitHub Settings access; this document is that instruction, not a
claim that it has already been done.

## `.github/workflows/main-extra.yml` — push to `main` only

- **migration-validation**: a real, disposable `pgvector/pgvector:pg16`
  service container, `alembic upgrade head` from empty, `alembic check`
  (fails on any ORM-model/schema drift), `scripts/migration_status.py`
  (fails on a multiple-heads/branched migration graph, a broken
  down_revision chain, or a database revision unknown to the repository —
  added Batch 4.6, after a real incident where a shared database's stamp
  pointed at a revision from two silently-diverged branches; see
  `docs/operations/migration-recovery.md`), a schema sanity check (pgvector
  extension + tables exist), then a real application startup against that
  database. Always against this job's own disposable service container —
  never the shared Neon development database.
- **docker-build**: builds both the backend/worker image and the frontend
  image (no push — no registry configured yet).

## `.github/workflows/e2e-and-eval.yml` — push to `main`, nightly, and manual

The two genuinely expensive tiers, each **independently gated** so this
workflow is safe to merge before its secrets exist (it skips, not fails):

- **browser-e2e** (`if: secrets.OPENAI_API_KEY != ''`): real Postgres +
  Redis service containers, real migrations (via `MIGRATION_DATABASE_URL`,
  the admin role), `scripts/verify_rls_isolation.py` (same tenant-isolation
  check `rls-security.yml` runs, as a belt-and-suspenders check against
  this workflow's own database — the mandatory, OpenAI-independent copy of
  this check lives in `rls-security.yml`, not here), `scripts/e2e_seed.py`,
  a real backend/worker/frontend connected via `DATABASE_URL` (`ekip_app`),
  then the full Playwright suite. Optionally exercises the real GitHub
  connector if `EKIP_TEST_GITHUB_TOKEN`/`EKIP_TEST_GITHUB_REPOS` secrets are
  also set (skips that one sub-test cleanly otherwise — see
  `critical-workflow.spec.ts`'s own `test.skip`). On failure, uploads the
  Playwright HTML report + traces/screenshots as a build artifact (14-day
  retention) — never uploaded on success.
- **ai-evaluation** (`if: secrets.EVAL_DATABASE_URL != '' && secrets.OPENAI_API_KEY != ''`):
  runs `scripts/eval_confidence.py` against a **real, persistent, pre-seeded**
  evaluation database (deliberately *not* the disposable per-run CI
  Postgres — the script needs the `test-org` golden corpus already
  ingested, which a fresh empty database doesn't have). Compares against
  `scripts/eval_confidence_report_after.json` and **fails the job on any
  regressed metric** — this batch changed `_compare_reports`/`main()` to
  actually propagate a non-zero exit code on regression; previously it only
  printed a warning.

- **status-summary** (`needs: [browser-e2e, ai-evaluation, semantic-benchmark]`,
  `if: always()`): writes an explicit `PASS`/`NOT RUN`/`FAIL` line per job to
  the job summary, so "OpenAI key missing, tier skipped" is never visually
  indistinguishable from "tier ran and passed." Only fails itself if a
  needed job's result is literally `failure` — a `skipped` result (missing
  secret) is reported as `NOT RUN`, not treated as a failure of this
  summary job itself. Tenant-isolation coverage that must always run lives
  in the separate `rls-security.yml` workflow, not here — this job's
  summary says so explicitly.

## Secret tiers

| Secret | Used by | Sensitivity |
|---|---|---|
| `OPENAI_API_KEY` | `browser-e2e`, `ai-evaluation` | Real cost per use — a real key |
| `EKIP_TEST_GITHUB_TOKEN` / `EKIP_TEST_GITHUB_REPOS` | `browser-e2e` (optional) | A real GitHub PAT scoped to disposable test repos only |
| `EVAL_DATABASE_URL` / `EVAL_REDIS_URL` | `ai-evaluation` | A real, persistent database — **not** a production database; a dedicated eval environment with the golden corpus already ingested |

None of these are required for `ci.yml` (the PR-blocking tier) to pass.
Configure them in the repository's Settings → Secrets → Actions. Never
commit any of them, never put them in workflow YAML directly, and never
reuse a production credential for `EVAL_DATABASE_URL` — it should be a
separate, dedicated evaluation environment.

## Known limitations

None of these four workflow files have been executed by GitHub Actions
itself yet (this environment has no way to trigger a real Actions run) —
each was written against this repo's actual, verified commands. Unlike the
original three, `rls-security.yml`'s actual execution path (service
container → `alembic upgrade head` via the admin role → `scripts/
verify_rls_isolation.py` via `ekip_app` → cross-tenant checks) was proven
end-to-end against a real, disposable, from-scratch Postgres + pgvector
instance in this environment (not GitHub-hosted, but genuinely executed —
not just YAML-parsed): a clean run passes every check, connecting as the
admin role for migrations and as `ekip_app` bypasses BYPASSRLS/superuser
checks and fails immediately as designed, and disabling RLS on a table
mid-run is independently caught by both the schema-level check and the
actual cross-tenant read. Building this proof also caught a real bug in the
migration/runtime role split shipped earlier the same day —
`run_migrations_online` was silently ignoring `migration_database_url` and
always running every migration as the runtime `ekip_app` role instead of
the admin role, in every topology, since the setting was introduced (see
`EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md` recommendation #2's "CRITICAL
CORRECTION" note) — now fixed in `app/database/session.py` and
`app/database/migrations/base.py`. GitHub's own runners will still be the
first confirmation of the *hosted* environment specifically (network
egress, `pgvector/pgvector:pg16` image pull, `astral-sh/setup-uv`
compatibility), but the migration/RLS logic itself is no longer unproven.
