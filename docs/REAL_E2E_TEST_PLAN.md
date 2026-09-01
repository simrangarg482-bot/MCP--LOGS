# EKIP real end-to-end test plan

This is the current, executable test path for a local production-shaped
stack. `docs/USER_TESTING_GUIDE.md` is retained as historical detail but
labels itself stale; this plan is intentionally narrower and maps directly
to the current compose services and Playwright configuration.

## What this proves

A successful run proves, against real PostgreSQL/pgvector, Redis, workers,
HTTP services, browser code and OpenAI calls:

- fresh migrations and application startup;
- signup/login, invitations and RBAC;
- application-layer tenant isolation plus a direct PostgreSQL RLS proof;
- connector registration and ingestion when optional GitHub credentials are supplied;
- search, confidence routing, grounded answers or honest escalation;
- incident investigation, timeline, postmortem review and audit records;
- knowledge review, accessibility and responsive UI behavior.

It does not prove production capacity, every connector, cloud backup/restore,
or source-document ACL parity. Those remain separate release gates.

## Safety boundaries

- Run only against the compose project `ekip-real-e2e` or another disposable staging environment.
- Never use production databases, connector credentials or customer content.
- Use a fine-grained GitHub token restricted to one disposable test repository, read-only contents/metadata access, and an expiry date.
- `.env.docker` and the root `.env` are gitignored. Never paste their values into reports or terminal commands.
- `.real-e2e-results/` is gitignored because Playwright traces and reports may contain ingested fixture text.
- `-Fresh` removes only Docker volumes owned by the explicitly named `ekip-real-e2e` compose project.

## One-time setup

1. Start Docker Desktop and wait until `docker info` succeeds.
2. From the repository root:

   ```powershell
   Copy-Item .env.docker.example .env.docker
   ```

3. Edit `.env.docker` and replace only `OPENAI_API_KEY` with a funded test key. Keep `ENVIRONMENT=development` and the local-only credentials unchanged.
4. Install dependencies if needed:

   ```powershell
   uv sync --extra dev
   Set-Location frontend
   npm ci
   Set-Location ..
   ```

5. Confirm preparation:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Preflight
   ```

## Run without an external connector

This validates every browser scenario except the real GitHub connect/sync
subtest, which Playwright records as skipped:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase All -Fresh
```

The script deliberately leaves the stack running for manual inspection at
`http://127.0.0.1:8080`. Stop it later with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Down
```

To also remove the isolated test database/Redis volumes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Down -RemoveData
```

## Run with a real GitHub connector

1. Create a disposable GitHub repository, for example `your-test-org/ekip-mercury-fixture`.
2. Copy the contents of `tests/real_e2e/fixture_repo/` into its repository root and commit them unchanged.
3. Create an expiring, fine-grained, read-only GitHub token scoped only to that repository.
4. Add these keys to the gitignored root `.env` without quoting or printing their values:

   ```dotenv
   EKIP_TEST_GITHUB_TOKEN=<fine-grained-test-token>
   EKIP_TEST_GITHUB_REPOS=your-test-org/ekip-mercury-fixture
   ```

5. Execute:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase All -Fresh -IncludeConnector
   ```

After Playwright connects and synchronizes the repository, the runner also
executes five controlled answer-quality canaries from
`tests/real_e2e/quality_cases.example.json`. Clear questions require cited
facts; conflicting and absent facts must investigate or decline.

## Expected automated evidence

Each invocation writes a timestamped folder under `.real-e2e-results/` with:

- commit and dirty-worktree status;
- Docker version and compose service state;
- `/health` and `/ready` responses;
- deterministic seed output;
- RLS isolation output;
- Playwright test list, console result, HTML report, traces/screenshots/video on failure;
- controlled answer-quality result when `-IncludeConnector` is used.

Do not mark the run passed unless all of these are true:

| Gate | Required result |
|---|---|
| Services | Postgres and Redis healthy; migrate exits 0; API, frontend and both workers running |
| Readiness | `/health` and `/ready` return HTTP 200; database and Redis report healthy |
| RLS | `RLS ISOLATION TEST: PASSED`, including fail-closed and concurrent checks |
| Browser E2E | 32 tests pass, except the explicitly optional connector test may be skipped when `-IncludeConnector` is absent |
| Connector run | With `-IncludeConnector`, a completed ingestion run processes fixture documents; a timeout/empty history is a failure, not an accepted limitation |
| Quality canaries | 5/5 pass; clear answers have supporting citations; conflicting/absent cases do not provide substantive answers |
| Secrets | No token, password or API key appears in committed files or shared evidence |

## Required human review

Automation checks terms and routes, not whether prose is genuinely good.
For each of the five canary questions, a reviewer must open the UI and record:

| Case | Correct conclusion? | Citation supports every factual claim? | Certainty appropriate? | Notes |
|---|---|---|---|---|
| service-owner | | | | |
| checkout-first-step | | | | |
| rollback-release | | | | |
| conflicting-retry-policy | | | | |
| absent-hr-policy | | | | |

Any unsupported factual sentence, cross-tenant visibility, secret exposure,
or confident answer to the conflicting/no-information cases is a release-blocking failure.

## Diagnosis commands

Run phases independently after fixing a failure:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Up
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Seed
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase Rls
powershell -ExecutionPolicy Bypass -File scripts/run_real_e2e.ps1 -Phase E2E -IncludeConnector
```

The runner leaves containers running after a failure. Inspect only the
relevant service and redact output before sharing it:

```powershell
docker compose -p ekip-real-e2e -f docker-compose.yml -f docker-compose.real-test.yml ps --all
docker compose -p ekip-real-e2e -f docker-compose.yml -f docker-compose.real-test.yml logs --tail 200 backend
docker compose -p ekip-real-e2e -f docker-compose.yml -f docker-compose.real-test.yml logs --tail 200 worker agents-worker
```
