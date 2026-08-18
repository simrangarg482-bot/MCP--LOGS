# CI pipeline

Three GitHub Actions workflows, split by cost/speed tier (Phase 3 Batch 4):

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
- **backend**: `uv sync --frozen --extra dev`, `pytest`, `lint-imports`.
- **frontend**: `npm ci`, `tsc --noEmit`, `eslint`, `vite build`.

This tier is intentionally the only one required to pass before merge —
everything else needs real infrastructure/secrets a contributor's PR
shouldn't be blocked on.

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
  Redis service containers, real migrations, `scripts/e2e_seed.py`, a real
  backend/worker/frontend, then the full Playwright suite. Optionally
  exercises the real GitHub connector if `EKIP_TEST_GITHUB_TOKEN`/
  `EKIP_TEST_GITHUB_REPOS` secrets are also set (skips that one sub-test
  cleanly otherwise — see `critical-workflow.spec.ts`'s own `test.skip`).
  On failure, uploads the Playwright HTML report + traces/screenshots as a
  build artifact (14-day retention) — never uploaded on success.
- **ai-evaluation** (`if: secrets.EVAL_DATABASE_URL != '' && secrets.OPENAI_API_KEY != ''`):
  runs `scripts/eval_confidence.py` against a **real, persistent, pre-seeded**
  evaluation database (deliberately *not* the disposable per-run CI
  Postgres — the script needs the `test-org` golden corpus already
  ingested, which a fresh empty database doesn't have). Compares against
  `scripts/eval_confidence_report_after.json` and **fails the job on any
  regressed metric** — this batch changed `_compare_reports`/`main()` to
  actually propagate a non-zero exit code on regression; previously it only
  printed a warning.

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

None of these three workflow files have been executed by GitHub Actions
itself yet (no `.github/workflows/` existed before this batch, and this
environment has no way to trigger a real Actions run) — each was written
against this repo's actual, verified commands (the exact `pytest`/`npm run`
invocations used successfully throughout this project) and had its YAML
syntax validated locally, but the first real run in GitHub's own runners
will be the first true end-to-end confirmation.
