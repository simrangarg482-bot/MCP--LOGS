# EKIP — Final Go-Live Runbook

**Audience**: an operator with real Neon, Azure, GitHub, Docker, Redis, and OpenAI
access, who does not need to have read this project's history to execute this.

**Rule that governs every phase below**: a phase is not "done" because its
commands ran without error — it is done when its own **Evidence to capture**
column has been captured and reviewed. Do not skip ahead because a later
phase "should" work; each phase's Preflight assumes the previous phase's
evidence is real.

**Do not run Phase D, E, G, or H against Neon/Azure without a human explicitly
saying so at that moment** — a prior "yes" to this runbook as a whole is not
standing authorization for each individual destructive step inside it.

---

## PHASE A — Preflight

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| A1 | `az account show` | Returns a real subscription | No output / login error | N/A (read-only) | Paste of JSON output (redact subscription id if policy requires) |
| A2 | `az group show --name <your-ekip-resource-group>` | Resource group exists, is **not** `rg-nextcare-purview-demo` | Group not found, or is `rg-nextcare-purview-demo` | N/A | Confirm the group name explicitly before Phase H |
| A3 | `gh auth status` | Authenticated to the correct repo | Not logged in | N/A | Terminal output |
| A4 | `docker version` | Client + server both respond | Daemon not running | N/A | Terminal output |
| A5 | `uv run pytest tests/ -q --deselect tests/ingestion_retrieval/test_connectors.py::test_one_connector` | `487 passed, 1 deselected` | Any failure | Fix before proceeding — do not run infra phases against a red backend | Full pytest summary line |
| A6 | `uv run lint-imports` | `Contracts: 7 kept, 0 broken` | Any broken contract | Fix before proceeding | Full output |
| A7 | `cd frontend && npm run typecheck && npm run lint && npm run build` | All three succeed | Any failure | Fix before proceeding | Terminal output of all three |
| A8 | `az bicep build --file infra/main.bicep --stdout > /dev/null` | No output (0 errors/warnings) | Any diagnostic | Fix `infra/main.bicep` before proceeding | Exit code |
| A9 | `uv run alembic heads` | Single head: `c5e2a9f4d7b3` | More than one head, or a different head | Do not proceed to Phase D/E until resolved | Command output |

**If any of A5–A9 fail, stop.** Everything below assumes this exact, currently-verified baseline.

---

## PHASE B — Credential rotation

Full narrative procedure and post-rotation checklist already live in
`docs/operations/security-incidents.md` — this phase is that checklist made
mechanical. **Never print or paste an actual secret value into a terminal
this runbook's evidence gets copied from.**

| Step | Command / action | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| B1 | Rotate Azure DevOps PAT via Azure DevOps → User Settings → Personal Access Tokens | New PAT issued, old one revoked | Console denies rotation | Retry; do not proceed with the old PAT | Token *name*/*expiry* only, never the value |
| B2 | Rotate Neon database password via Neon console → connection settings | New password issued | Console denies | N/A | Confirmation screenshot with value redacted |
| B3 | Rotate Redis password via Redis Cloud console | New password issued | Console denies | N/A | Confirmation screenshot with value redacted |
| B4 | Update every place each credential is configured: local `.env`, GitHub repo secrets, and (once Phase H exists) the Key Vault secret / Container Apps `secretRef` backing it | All updated | Any location missed | Re-check the full list before continuing | Diff or change-log of which locations were updated, not the values |
| B5 | Restart/redeploy the application with the new values | Application starts | Startup failure | Revert to old credential only long enough to diagnose, then retry rotation | `/health` and `/ready` both 200 |
| B6 | `gitleaks detect --source . --config .gitleaks.toml --redact -v` | No findings | A finding appears | Investigate immediately — a finding here means a secret was pasted into a committed file during this rotation | Full gitleaks output (already redacted) |
| B7 | Grep the repo and any exported CI logs for each old credential's distinguishing *prefix* (never the value) | No matches | A match found | Remove the stale reference | Grep command + "no matches" confirmation |

**If Azure DevOps/Neon/Redis console access is unavailable to whoever runs
this: mark ROTATION BLOCKED and stop this phase — do not substitute a
different credential or skip ahead.**

---

## PHASE C — Database backup

Read-only against Neon; safe to run without further authorization, but still
worth doing deliberately rather than reflexively, since it's the gate every
later Neon-touching phase depends on.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| C1 | `pg_dump "$DATABASE_URL" -Fc -f ekip_neon_full_$(date -u +%Y%m%dT%H%M%SZ).dump` | A non-empty `.dump` file | Connection/auth error | N/A | File size + timestamp |
| C2 | `pg_dump "$DATABASE_URL" --schema-only -f ekip_neon_schema_$(date -u +%Y%m%dT%H%M%SZ).sql` | A non-empty schema-only `.sql` file | Connection/auth error | N/A | File size + timestamp |
| C3 | Store both artifacts somewhere durable *outside* this working directory (they contain real tenant data) | Confirmed stored | N/A | N/A | Storage location (not the files themselves, if this runbook's evidence trail is shared) |

**Do not proceed to Phase D without both C1 and C2 in hand.**

---

## PHASE D — Neon migration recovery

**Known current problem** (re-confirm with D1 before doing anything —
don't trust this runbook's memory of it): Neon's `alembic_version` was last
observed at `b3d8f1a6c9e2`, a revision absent from this repository's own
migration history entirely (see `docs/operations/migration-recovery.md` for
the full forensic investigation). Repository head is `c5e2a9f4d7b3`.

**Do not run D5 without a human explicitly re-confirming at that moment** —
this is the first step in this phase that writes to Neon.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| D1 | `uv run python scripts/migration_status.py` | Reports the current `Database revision` and whether it `Revision exists?` in this repo's history | Script errors | Fix connectivity before proceeding | Full output |
| D2 | Compare D1's output against `docs/operations/migration-recovery.md`'s findings | Matches the documented `b3d8f1a6c9e2` orphan, or reveals something has changed since that doc was written | Output contradicts the doc | **Stop.** Re-investigate before trusting the existing recovery plan — schema state may have moved since `migration-recovery.md` was authored | Diff between D1's output and the doc's claims |
| D3 | Re-read `docs/operations/migration-recovery.md` and `docs/operations/neon-recovery-plan.md` in full | Understand exactly which objects the recovery migration (`90ff736ced55`) drops and which it deliberately leaves (`ekip_app`, `incident:read`/`postmortem:read` permission rows) | N/A | N/A | Confirmation the reviewer read both documents |
| D4 | Review `app/database/migrations/versions/90ff736ced55_*.py` and every migration between it and `c5e2a9f4d7b3` line by line | Confirms the SQL matches what D2/D3 expect | Anything looks different from what the docs describe | **Stop** — do not proceed | Reviewer sign-off (name + date) |
| D5 | **[HUMAN CONFIRMATION REQUIRED]** `EKIP_APP_ROLE_PASSWORD=<new, freshly-rotated-in-Phase-B value> uv run alembic upgrade head` against the real Neon `DATABASE_URL` | Upgrades cleanly through every migration including `90ff736ced55` (recovery), `b8f3d6a1c4e7` (`ekip_app` provisioning), `c5e2a9f4d7b3` (`resolve_user_first_organization`) | Any migration errors partway through | Restore from Phase C's `pg_dump -Fc` backup via `pg_restore --clean` before retrying | Full `alembic upgrade` output, start-to-finish |
| D6 | `uv run alembic check` | No drift reported | Drift reported | Investigate before certifying — do not proceed to Phase E | Full output |
| D7 | `uv run python scripts/migration_status.py` (re-run) | `Database revision` now equals `c5e2a9f4d7b3`, `Revision exists? True` | Anything else | Investigate | Full output |
| D8 | Application smoke test: start the backend against this Neon instance, hit `/health`, `/ready`, attempt one real login | All succeed | Any failure | Restore from backup, investigate before retrying | Response bodies/status codes |

---

## PHASE E — `ekip_app` runtime role

This phase is largely **already executed by Phase D5** (the migration that
provisions `ekip_app` is part of the same `alembic upgrade head` run) — this
phase is about switching the *application's own connection* to use it, which
no migration can do by itself.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| E1 | Against Neon (or `psql`), `\du ekip_app` | Shows `Superuser: no`, `Bypass RLS: no`, `Create role: no`, `Create DB: no` | Any attribute wrong | Re-run `b8f3d6a1c4e7`'s `upgrade()` logic (it's idempotent — safe to re-run) | `\du` output |
| E2 | Confirm `neondb_owner` (or whichever role currently owns the schema) still exists and is untouched | Unchanged | Role missing/altered | Restore from Phase C backup | `\du neondb_owner` output |
| E3 | **[HUMAN CONFIRMATION REQUIRED]** Update the deployed `DATABASE_URL` secret (wherever the running application reads it — local `.env`, CI secret, or Phase H's Key Vault/Container Apps secret) to connect as `ekip_app` with the password set in D5 | Application's connection string now targets `ekip_app`, not the admin/owner role | N/A | Revert `DATABASE_URL` to the admin role, investigate | Redacted diff showing only the *username* changed, not full connection string |
| E4 | Restart the application; attempt a real password login end-to-end | Login succeeds — this specifically exercises `resolve_user_first_organization` (`c5e2a9f4d7b3`), the exact bootstrap function this whole role-switch would break without | Login fails, especially with a permissions/RLS-shaped error | Revert `DATABASE_URL` to the admin role immediately, investigate before retrying — this is the single most likely real-world failure point of this entire migration | Login response + backend logs for that request |
| E5 | Attempt one real read of each of: an incident, an audit log entry, a knowledge document, a connector config | All succeed and return only same-organization data | Any 403/empty-when-shouldn't-be / cross-org leak | Revert `DATABASE_URL`, investigate | Response bodies (redact tenant data if sharing this evidence externally) |
| E6 | Confirm the **migration** identity (admin/`neondb_owner`) is never present in the running application's own configuration — only in whatever ran D5/Phase H's `migrateJob` | Admin credential absent from backend/worker's actual env | Admin credential found in a running container's env | Remove it, redeploy | `env | grep -i database_url` inside a running backend container, or the Azure Container Apps secret list |

**Only after E4 and E5 both succeed does the application layer of "RLS is
active" hold.** E1–E3 alone are necessary but not sufficient.

---

## PHASE F — RLS validation

**Never run this against Neon.** `scripts/rls_isolation_test.py`'s own
docstring explains why: Neon's admin role bypasses RLS, so every assertion
would trivially pass regardless of whether RLS actually works — proving
nothing. This phase requires a genuinely disposable Postgres+pgvector
instance (local Docker, a throwaway cloud instance, or CI).

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| F1 | Stand up a disposable Postgres 16 + pgvector instance (e.g. `docker run` the `pgvector/pgvector:pg16` image, or a throwaway cloud instance) | Reachable via a connection string | Can't provision one | Mark **RLS ISOLATION = BLOCKED**, stop this phase | Connection details (host only, not credentials) |
| F2 | `DATABASE_URL=<admin conn string to F1> EKIP_APP_ROLE_PASSWORD=<any throwaway value> uv run alembic upgrade head` | Every migration applies cleanly to a genuinely empty database, including `ekip_app` provisioning | Any migration error | Fix before proceeding | Full output |
| F3 | `RLS_TEST_DATABASE_URL=<admin conn string to F1> uv run python scripts/rls_isolation_test.py` | Prints `RLS ISOLATION TEST: PASSED` with all four sub-checks OK (basic contract, pooled-connection reuse, fail-closed with no context, concurrent cross-org queries) | Prints `RLS ISOLATION TEST: FAILED` with one or more listed failures | **Do not proceed past this phase** — RLS is not provably working; investigate the specific failure against `app/database/migrations/versions/c7d4e8f19a2b_*.py`'s policies before retrying anywhere real | Full script output, verbatim |
| F4 | Manually verify the specific matrix this runbook requires: Alpha→Alpha ALLOW, Alpha→Beta DENY, Beta→Beta ALLOW, Beta→Alpha DENY | All four hold (F3's own assertions already cover this — this step is a human re-read of F3's output line by line, not a new script) | Any cell of the matrix wrong | Same as F3 | Annotated F3 output confirming each of the four cells |
| F5 | **Known scope gap, disclose rather than silently extend**: `rls_isolation_test.py` as it exists today directly exercises incidents and the four checks above; it does not yet have dedicated assertions for timeline entries, audit log rows, connector configs, knowledge documents, or postmortems specifically, or a real arq worker process. If broader per-table coverage is required before sign-off, extend the script (following its own existing pattern) rather than assuming the incidents-table result generalizes — `c7d4e8f19a2b`'s policies are structurally identical across `_DIRECT_TABLES`, but "structurally identical" is exactly the kind of claim this whole runbook exists to stop taking on faith | Either accept the current scope explicitly, or extend the script and re-run | N/A | Reviewer's explicit decision, recorded |

**RLS may be marked PASS in Phase O only if F3 was actually run and printed
`PASSED` — never from reading the policy SQL, never from Phase E5's
apparent success alone (E5 proves tenant isolation held for the specific
rows tested in that one session; F3 proves the *mechanism* fails closed
under adversarial conditions Phase E doesn't exercise).**

---

## PHASE G — Azure Key Vault

| Step | Command / check | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| G1 | Confirm the target resource group is **not** `rg-nextcare-purview-demo` and is one this operator is actually authorized to provision EKIP infrastructure into | Confirmed | Wrong resource group | Stop, get the right one | Resource group name + confirmation of authorization |
| G2 | After Phase H provisions it: `az keyvault show --name <prefix>-kv` | Vault exists, `enableRbacAuthorization: true`, `enablePurgeProtection: true` | Any of those false, or vault missing | Redeploy via Bicep, do not hand-configure a vault outside the template | `az keyvault show` output |
| G3 | `az role assignment list --scope <vault resource id>` | Exactly two role assignments to `appIdentity`: **Key Vault Crypto User** (`e147488a-f6f5-4113-8e2d-b22465e65bf6`) and **Key Vault Secrets User** (`4633458b-17de-408a-b874-0445c86b69e6`) — nothing else | Any additional role (Owner, Contributor, Manage Access Policies, or anything broader) present | Remove the excess role assignment immediately | Full role assignment list |
| G4 | Confirm no `Delete`/`Purge` permission was granted anywhere in this list | Confirmed absent | Present | Remove it | Same G3 output, annotated |
| G5 | Application-level wrap/unwrap test: register one real connector through the running application (against this real vault) | Connector's `credential_ref` is envelope-encrypted; `app.shared.security.kms.AzureKeyVaultKeyManagementService` successfully wraps at write and unwraps at the next sync | Wrap or unwrap fails | Check `AZURE_KEY_VAULT_URL`/`AZURE_KEY_VAULT_KEY_NAME` env vars and the managed identity's role assignment | Connector registration success response; a completed (not failed) sync run afterward |
| G6 | Same for an SSO configuration (`POST /organizations/{id}/sso/configure`) | `client_secret_ref` round-trips correctly through the same KMS path | Failure | Same as G5 | SSO config creation success; a subsequent `GET` showing the redacted placeholder, not the plaintext or an error |
| G7 | Same for an MCP OAuth client secret, if this deployment registers one | Round-trips correctly | Failure | Same as G5 | Registration success + one successful token exchange |
| G8 | Key version handling: rotate the Key Vault key (`az keyvault key rotate` or create a new version), then attempt to *read* (not re-write) a connector credential created before rotation | Still decrypts correctly — `app.shared.security.kms`'s envelope format is documented to carry its own key-version reference | Decryption fails after rotation | This is a real bug in the envelope format if it happens — do not paper over it, file it | Before/after key version id + successful decrypt confirmation |
| G9 | Permission-denial test: from a context that does **not** hold the managed identity (e.g. your own `az login` session, if it lacks the Crypto User/Secrets User roles), attempt to read a secret or use the key directly | Denied | Succeeds | This would mean the RBAC scoping is broader than intended — investigate immediately | The denial error message |
| G10 | `production` environment guard: with `ENVIRONMENT=production` and `KMS_PROVIDER=local` set, start the application | Refuses to start — `app/shared/config/settings.py`'s `_reject_local_kms_in_production` validator raises at settings-construction time | Application starts anyway | This is a real regression — stop and investigate before deploying anything | Startup failure log showing the exact `ValueError` message |

---

## PHASE H — Azure deployment

Uses `infra/main.bicep` and `scripts/deploy.sh`. **Confirm the resource group
one more time before H2 — this is the step that actually creates billable
resources.**

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| H1 | `az account show` and `az group show --name <your-ekip-rg>` | Correct subscription and resource group, confirmed **not** `rg-nextcare-purview-demo` | Wrong account/group | Stop | Both command outputs |
| H2 | **[HUMAN CONFIRMATION REQUIRED]** `az deployment group create --resource-group <rg> --template-file infra/main.bicep --parameters infra/main.parameters.example.json --parameters postgresAdminPassword=$PG_PW ekipAppPassword=$EKIP_APP_PW openAiApiKey=$OPENAI_KEY` | Deployment succeeds; 15 resources created (managed identity, Key Vault + key + 2 role assignments + 1 secret, Postgres + database + extension config, Redis, Container Apps environment, migrate job, backend/worker/frontend apps) | Deployment fails partway | `az deployment group delete` or targeted resource cleanup; do not leave partial infrastructure running unbilled-but-unused | Full `az deployment group create` output + `az deployment group show` afterward |
| H3 | `az containerapp job start --name <prefix>-migrate --resource-group <rg>` | Job runs `alembic upgrade head` against the newly-provisioned Postgres and exits 0 | Non-zero exit | Check job logs (`az containerapp job logs show`); this is the same migration chain already verified in Phase D/F, so a failure here likely means a config mismatch (wrong `DATABASE_URL`/`EKIP_APP_ROLE_PASSWORD`), not a migration bug | Job execution status + logs |
| H4 | `az containerapp update --name <prefix>-backend ...` / `-worker` / `-frontend`, each with the built image | All three deploy | Any fails | `az containerapp revision list` to roll back to the prior revision (see `docs/operations/rollback.md`) | Revision list showing the new revision active |
| H5 | `curl https://<backend fqdn>/health` | `{"status": "ok"}` | Non-200 or wrong body | Roll back per `docs/operations/rollback.md` | Response body + status code |
| H6 | `curl https://<backend fqdn>/ready` | `{"status": "ready", "database": {"status": "ok"}, ...}` | `not_ready` or `database.status != "ok"` | Roll back; this specifically means Phase E's runtime role isn't correctly wired | Response body + status code |
| H7 | Confirm via `az containerapp show --name <prefix>-backend` that its env vars include `DATABASE_URL`/`OPENAI_API_KEY`/`REDIS_URL` as `secretRef`s, never plain values | All three are `secretRef` | Any is a plain value | Redeploy — this would mean a real regression in `infra/main.bicep` | `az containerapp show` env var section |
| H8 | Confirm the migration/admin credential (`postgresAdminPassword`) is **not** present in `backendApp`/`workerApp`'s secrets — only in `migrateJob`'s | Confirmed absent from the two runtime apps | Present | Redeploy with a fixed template | `az containerapp show`'s secret *names* list (never dump secret values) for all three resources |

---

## PHASE I — Docker validation

If Docker is unavailable to whoever runs this, mark **DOCKER = BLOCKED,
static-only** and skip to Phase J — do not fabricate results.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| I1 | `cp .env.docker.example .env.docker` and fill in a real `OPENAI_API_KEY` | File created | N/A | N/A | Confirmation only, never the file's contents |
| I2 | `docker compose build` | All five images build | Any build failure | Fix Dockerfile/compose, rebuild | Build log tail for each image |
| I3 | `docker compose up` | `postgres`/`redis` become healthy, `migrate` runs and exits 0, `backend`/`worker`/`frontend` start after | Any service fails to start, or `migrate` exits non-zero | `docker compose logs migrate` for the specific error | `docker compose ps` showing final state of all 5 services |
| I4 | `curl http://localhost:8000/health` and `.../ready` | Both 200 | Non-200 | Investigate via `docker compose logs backend` | Response bodies |
| I5 | `docker compose exec backend python -c "print(1)"` then `uv run pytest tests/ -q` from a shell with the compose network reachable, or run the suite against the compose Postgres directly | Tests pass against the real containerized stack (distinct from Phase A5's mocked-DB run) | Failures | Investigate — this is a different signal than A5 | Test summary |
| I6 | `docker compose logs backend worker | grep -iE "password|secret|token|traceback"` | No secret values in output; any tracebacks investigated | A real secret value appears in logs | This is itself a security finding — fix the logging call that leaked it before proceeding | Redacted grep output (or "no matches") |
| I7 | `docker history ekip-backend:latest` / `ekip-frontend:latest` | No layer contains `.env`, a real API key, or a private key | Any layer does | Rebuild with a `.dockerignore` fix, this is a real image-hygiene bug | `docker history` output |

---

## PHASE J — CI/CD validation

Do not infer success from workflow YAML being well-formed — this phase
requires an actual run.

| Step | Action | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| J1 | Push a commit (even a trivial one) to `main`, or `gh workflow run ci.yml` | Workflow triggers | Doesn't trigger | Check workflow `on:` triggers | Run URL |
| J2 | `gh run watch <run-id>` or the GitHub Actions UI | `secret-scan`, `backend`, `frontend` jobs (from `ci.yml`) all succeed | Any fails | Fix the underlying issue, re-run | Run URL + final conclusion per job |
| J3 | Confirm `main-extra.yml` triggered on the same push | `migration-validation` (disposable Postgres, `alembic upgrade head`, `alembic check`, `scripts/migration_status.py`, pgvector check, app-starts check) and `docker-build` both succeed | Either fails | Fix, re-run — a `migration-validation` failure here means this session's new migrations (`b8f3d6a1c4e7`/`c5e2a9f4d7b3`) have a real bug the earlier static Bicep-only checks couldn't catch | Run URL + job logs |
| J4 | If `OPENAI_API_KEY` is configured as a repo secret, confirm `e2e-and-eval.yml`'s `browser-e2e` job also ran (rather than skipping) | Runs and reports real pass/fail counts | Skips (secret not configured) or fails | If skipped: expected, not a failure — note it. If failed: see Phase L | Run URL |
| J5 | Record for the certification report: workflow run URL, commit SHA, and final status for every job in J2–J4 | Recorded | N/A | N/A | The actual table, not a summary claim |

---

## PHASE K — AI evaluation

Only run this with a live database containing real ingested data (the
`test-org` golden corpus `scripts/eval_confidence.py` expects) and a funded
`OPENAI_API_KEY` — it costs real money per run.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| K1 | Confirm the target database actually has the `test-org` corpus ingested (`--org-slug test-org` is the script's default) | Confirmed via a quick row-count query or the app's own knowledge-list endpoint | Empty/wrong org | Ingest the corpus first, or point `--org-slug` at whichever org actually has it | Row count or endpoint response |
| K2 | `uv run python scripts/eval_confidence.py --report-path eval_confidence_report.json` | Completes, writes a report with real precision/recall/routing numbers per question category | Script errors (missing DB/API key) | Fix connectivity, retry | Full report JSON |
| K3 | `uv run python scripts/eval_confidence.py --report-path eval_confidence_report.json --compare-to scripts/eval_confidence_report_after.json` | Compares against the last known-good baseline; exits 0 if no regression | Non-zero exit (regression detected) | **Do not certify AI evaluation as PASS** — investigate the specific regressed category before proceeding | Full comparison output, including the exact ambiguous-false-answer rate this run measured |
| K4 | Record the actual measured ambiguous false-answer rate | A real number from K3's output | N/A | N/A | The number itself — **never write "0.333 → 0.083" in the certification report unless K3's own output says so for this run** |

---

## PHASE L — Full Playwright E2E

No mocks — this must run against the real stack from Phase H or Phase I.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| L1 | Confirm a real backend + frontend + Postgres + Redis stack is reachable (Phase H's deployed URLs, or Phase I's local compose stack) | Reachable | Not reachable | Fix Phase H/I first | Health check response |
| L2 | `uv run python scripts/e2e_seed.py` against that stack's database | Seeds `ORG_ALPHA`/`ORG_BETA` test users deterministically | Script errors | Fix DB connectivity | Script output |
| L3 | `cd frontend && npx playwright test` | Runs the complete suite | N/A (see L4 for interpreting results) | N/A | Full console output |
| L4 | Record: test file, test count, passed, failed, skipped, browser, commit SHA, environment (Phase H Azure URL or Phase I local) | A real, filled-in table — see below | Any test failed and wasn't triaged | Fix the underlying app bug or the test itself, re-run, don't just retry until green without understanding why one failed | The filled table, plus `frontend/playwright-report/` artifact |

Required coverage (confirm each maps to an actual spec file that ran, not
just that the suite as a whole passed):

| Area | Spec file (expected) |
|---|---|
| Auth (signup/login/session expiry) | `frontend/e2e/*.spec.ts` covering `signupViaUI`/`loginViaUI`/token expiry |
| Invitation acceptance | `frontend/e2e/accept-invitation.spec.ts` |
| RBAC / tenant isolation | `frontend/e2e/rbac.spec.ts`, `tenant-isolation.spec.ts` |
| Incident / timeline | `frontend/e2e/critical-workflow.spec.ts` or equivalent |
| Search / Ask / citation / evidence | same |
| Investigation | same |
| Postmortem | same |
| Knowledge Review | `frontend/e2e/knowledge-review.spec.ts` |
| Connectors / ingestion | `critical-workflow.spec.ts`'s connector step (self-skips without `EKIP_TEST_GITHUB_TOKEN`) |
| Audit | same |
| SSO | (no dedicated spec confirmed as of this runbook — verify one exists or note the gap) |
| Responsive | `frontend/e2e/responsive.spec.ts` |
| Accessibility | `frontend/e2e/accessibility.spec.ts` |

**Report exact `X/X passed`. A skip is not a pass. Do not average across runs
— report the one real run this phase produced.**

---

## PHASE M — Backup / restore

Uses Phase C's real backup.

| Step | Command | Expected result | Failure condition | Rollback | Evidence to capture |
|---|---|---|---|---|---|
| M1 | Provision a fresh, disposable Postgres instance (separate from Phase F's, or reuse it if already torn down) | Reachable | Can't provision | Mark **BACKUP/RESTORE = BLOCKED** | Connection details |
| M2 | `pg_restore --clean --if-exists -d <disposable target> ekip_neon_full_<timestamp>.dump` (Phase C1's artifact) | Restores without fatal errors | Fatal restore error | Investigate the dump/restore compatibility (Postgres version match) | Full restore log |
| M3 | `uv run alembic current` against the restored database | Matches whatever revision Phase C's backup was taken at | Mismatch | Investigate | Command output |
| M4 | Start the application against the restored database; `/health`, `/ready` | Both succeed | Failure | Investigate | Response bodies |
| M5 | Query one real row from each of: incidents, users, organizations | Data present and matches what existed at backup time | Missing/corrupted data | Restore failed — investigate `pg_dump`/`pg_restore` version compatibility | Query results |
| M6 | If Phase E/F already ran against this restored copy, re-run Phase F3 (`rls_isolation_test.py`) against it | Still passes | Fails | The restore process itself may have dropped role/RLS state — investigate | F3-style output |
| M7 | Attempt one real end-to-end workflow (login → ask a question → view an incident) against the restored copy | Succeeds | Failure | Investigate | Screenshots or response bodies |

---

## PHASE N — Security sign-off

| Step | Command | Expected result | Failure condition | Evidence to capture |
|---|---|---|---|---|
| N1 | `uv run pytest tests/ -q --deselect tests/ingestion_retrieval/test_connectors.py::test_one_connector` | `487 passed, 1 deselected` (or higher, if more tests were added during this runbook's execution) | Any failure | Full summary |
| N2 | `uv run lint-imports` | `7 kept, 0 broken` | Any broken | Full output |
| N3 | `gitleaks detect --source . --config .gitleaks.toml --redact -v` | No findings | A finding | Redacted output |
| N4 | Phase F3's `rls_isolation_test.py` result | `PASSED` | `FAILED` | Reference to Phase F's own evidence |
| N5 | Re-run this session's API contract audit spot-checks (do **not** redo the full audit — confirm the specific fixes already made are still in place): SSO secret field is `type="password"`, invitation role field is free-text, `Project.slug` is not referenced anywhere | All three still hold | Any regressed | `grep` confirmation for each |
| N6 | `cd frontend && npm run typecheck && npm run lint && npm run build` | All pass | Any fails | Terminal output |
| N7 | Prompt-injection / SSRF / OAuth / CORS: confirm `app/agents/prompt_safety.py` is still imported by the same 8 agent modules, `assert_safe_connector_url` still wired into `confluence.py`/`jira.py`, `_assert_redirect_uri_allowed` still called from both SSO entry points, CORS wildcard-with-credentials still rejected | All confirmed via `grep`, not re-audited from scratch | Any missing | Grep output for each |
| N8 | Rate limiting: confirm `tests/api/test_rate_limit.py` still passes (already part of N1's full suite) | Passing | Failing | Test name + result from N1 |

**Required: ZERO Critical, ZERO High.** If RLS is inactive (Phase E/F not
both complete and passing), if credentials are unrotated (Phase B not
complete), or if tenant isolation is not empirically verified (Phase F3 not
run and passing) — this phase **FAILS**, regardless of how clean N1–N8 look
in isolation.

---

## PHASE O — Production certification

1. Fill in `docs/operations/production-release-checklist.md` completely, with real evidence references (not just checkmarks) for every item.
2. Update `docs/PROJECT_STATUS.md` to reflect the real, current state after this runbook's execution.
3. Produce `docs/operations/FINAL_PRODUCTION_CERTIFICATION.md` using the fixed structure that file already has, with a verdict of `PRODUCTION READY` only if every item in that file's own release-gate section is empirically true — not "should be true given the code."
4. If anything in Phases A–N was marked BLOCKED, FAILED, or skipped, the certification **must** say `NOT PRODUCTION READY`, regardless of how many other phases passed.
