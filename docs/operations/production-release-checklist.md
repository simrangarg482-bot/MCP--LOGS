# EKIP — Production Release Checklist

Each item below maps to a phase in `docs/operations/final-go-live-runbook.md`.
**Do not check an item based on code review alone** — check it only once the
referenced phase's evidence has actually been captured. An unchecked item
blocks `PRODUCTION READY` in `docs/operations/FINAL_PRODUCTION_CERTIFICATION.md`
regardless of how many other items are checked.

As of this checklist's creation (2026-08-18), **every item below is unchecked**
— all code-level prerequisite work is complete (see `docs/PROJECT_STATUS.md`),
but none of the infrastructure-dependent items have been executed in this
environment (no Docker, no disposable Postgres, no `gh`/Azure deployment
authorization, no live OpenAI-funded corpus).

- [ ] **Credentials rotated** — Runbook Phase B. Evidence: rotation confirmation for Azure DevOps PAT, Neon password, Redis password; post-rotation gitleaks scan clean; no stale credential references found.
- [ ] **Neon backed up** — Runbook Phase C. Evidence: `pg_dump -Fc` full backup + schema-only dump, both stored durably outside this working directory.
- [ ] **Neon migration repaired** — Runbook Phase D. Evidence: `scripts/migration_status.py` reports `Database revision` = `c5e2a9f4d7b3`, `Revision exists? True`.
- [ ] **Fresh migration verified** — Runbook Phase F2 (or CI's `main-extra.yml` `migration-validation` job). Evidence: `alembic upgrade head` against a genuinely empty database succeeds; `alembic check` reports no drift.
- [ ] **`ekip_app` provisioned** — Runbook Phase D5/E1. Evidence: `\du ekip_app` on the target database shows the role exists with the correct attributes.
- [ ] **Runtime uses `ekip_app`** — Runbook Phase E3. Evidence: the deployed `DATABASE_URL` secret (Key Vault / Container Apps secret / `.env`) authenticates as `ekip_app`, confirmed via `az containerapp show`'s secret list (names only) or equivalent, not the admin/owner role.
- [ ] **`ekip_app` has `NOBYPASSRLS`** — Runbook Phase E1. Evidence: `\du ekip_app` shows `Bypass RLS: no` (and `Superuser: no`, `Create role: no`, `Create DB: no`).
- [ ] **RLS isolation verified** — Runbook Phase F3. Evidence: `scripts/rls_isolation_test.py` run against a disposable Postgres+pgvector instance, printed `RLS ISOLATION TEST: PASSED`, all four sub-checks OK. **Never satisfied by a run against Neon.**
- [ ] **Connection-pool isolation verified** — Runbook Phase F3 ("pooled-connection reuse (Alpha → Beta → Alpha)" sub-check). Evidence: same run as above, that specific line printed OK.
- [ ] **Worker isolation verified** — Runbook Phase F5 (known scope gap — the current script does not yet exercise a real arq worker process directly; extend it first, or explicitly accept the gap and record that decision here).
- [ ] **Azure deployed** — Runbook Phase H. Evidence: `az deployment group create` succeeded against a real, EKIP-authorized resource group (never `rg-nextcare-purview-demo`); 15 resources created; migration job ran; `/health`/`/ready` both green on the deployed backend URL.
- [ ] **Key Vault verified** — Runbook Phase G. Evidence: RBAC role list shows exactly Key Vault Crypto User + Key Vault Secrets User on the managed identity, nothing broader; real wrap/unwrap exercised via a real connector/SSO/MCP OAuth secret; `production` + `KMS_PROVIDER=local` confirmed to refuse startup.
- [ ] **Docker verified** — Runbook Phase I. Evidence: `docker compose up` brought up all 5 services healthy; tests ran against the real containerized stack; no secrets found in image layers or logs.
- [ ] **CI verified** — Runbook Phase J. Evidence: real workflow run URLs + commit SHA + per-job pass/fail for `ci.yml`'s `secret-scan`/`backend`/`frontend` and `main-extra.yml`'s `migration-validation`/`docker-build`.
- [ ] **AI evaluation verified** — Runbook Phase K. Evidence: `scripts/eval_confidence.py` run against a live, real-corpus database with a funded OpenAI key; real measured ambiguous-false-answer rate recorded (not assumed to still be 0.333 → 0.083 from a prior run).
- [ ] **E2E verified** — Runbook Phase L. Evidence: real `npx playwright test` run against the real stack; exact `X/X passed` recorded per the coverage table in Phase L, not "tests exist" or "tests compile."
- [ ] **Backup verified** — Runbook Phase C (same artifact as "Neon backed up" above — listed separately here because "took a backup" and "the backup is usable" are different claims).
- [ ] **Restore verified** — Runbook Phase M. Evidence: Phase C's backup actually restored into a disposable instance; schema, data, application startup, migration state, and one real end-to-end workflow all confirmed against the restored copy.
- [ ] **Security scan verified** — Runbook Phase N. Evidence: `gitleaks` clean, `pytest`/`lint-imports` green, RLS/contract/prompt-injection/SSRF/OAuth/CORS spot-checks all confirmed still in place.
- [ ] **Frontend regression verified** — Runbook Phase A7/N6. Evidence: `typecheck`/`lint`/`build` all pass on the exact commit being certified.
- [ ] **Backend regression verified** — Runbook Phase A5/N1. Evidence: full pytest suite passing on the exact commit being certified.
- [ ] **Documentation verified** — `docs/PROJECT_STATUS.md` and this checklist both reflect the real, current, post-runbook state — no stale claims, no phase marked complete without a corresponding evidence reference above.
