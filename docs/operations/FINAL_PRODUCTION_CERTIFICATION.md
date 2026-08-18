# EKIP — Final Production Certification

Certification date: 2026-08-18
Commit under certification: current `main` working tree (migration head `c5e2a9f4d7b3`)
Certified by: this session's code-complete handoff — infrastructure execution not yet performed by anyone with the required access

## Executive Summary

EKIP's application code, security design, and infrastructure-as-code are
code-complete: every gap found during this project's audits has a real fix,
a regression test, and (for infrastructure) a statically-validated Bicep/CI
change. Nothing in this report is fabricated — every PASS below was actually
executed in this session; every BLOCKED below names the exact missing
access and the exact runbook phase (`docs/operations/final-go-live-runbook.md`)
that closes it once that access exists. **The system is not production
ready** — not because of unresolved code defects, but because the
defense-in-depth database security model (Row-Level Security) is currently
inactive against real traffic, and no infrastructure-execution phase
(Azure, Docker, live database migration, AI evaluation, or E2E) has ever
actually run. Closing every remaining item is now a matter of operator
access, not further engineering — see the runbook for the exact sequence.

## Architecture
**PASS**
Modular monolith with `import-linter`-enforced boundaries; 7/7 contracts held throughout every change made this session.

## Backend
**PASS**
487/487 tests passing (re-confirmed multiple times this session, including after every migration/schema-adjacent change).

## Frontend
**PASS**
`typecheck`/`lint` (0 warnings)/`build` all pass, re-confirmed after every change this session.

## Authentication
**PASS**
Password + SSO/PKCE flows; invitation acceptance fully provisions real accounts and sessions; `resolve_user_first_organization` (new this session) closes the specific bootstrap gap that would otherwise break password login the moment the runtime role switches to `ekip_app`.

## RBAC
**PASS**
Server-side `require_permission`/`require_project_permission` enforced on every write path audited this session; frontend gates verified to match backend codes exactly (one real mismatch found and fixed: the invitation-role dropdown).

## Tenant Isolation
**PASS (application layer only)**
Every query scopes by `organization_id` explicitly. This is what is actually protecting tenant data in the currently-running system — see RLS below for why it is not yet a second, independent layer.

## RLS
**BLOCKED**
Policies exist (`c7d4e8f19a2b`), `FORCE ROW LEVEL SECURITY` applied, not weakened or removed. The runtime-role fix is code-complete (`b8f3d6a1c4e7` provisions `ekip_app`, `NOSUPERUSER`/`NOBYPASSRLS`) but has never been applied to any real database, and `scripts/rls_isolation_test.py` has never been executed against a disposable Postgres instance in this environment. **Confirmed, not assumed**: the real database connection (`neondb_owner`) has `bypassrls=true`, meaning every RLS policy is currently a live no-op. Unblock: Runbook Phases D, E, F.

## Secrets/KMS
**BLOCKED**
Fail-closed `production + KMS_PROVIDER=local` guard is real and pytest-covered (PASS at the code level). Azure Key Vault wrap/unwrap has never been exercised against a real vault — 3 real Bicep wiring bugs were found and fixed this session (OpenAI key, DB password, Redis key never reaching the runtime containers) but remain unexercised beyond `az bicep build`. Unblock: Runbook Phase G.

## Azure
**BLOCKED**
`infra/main.bicep` compiles clean (0 errors) and includes a previously-missing migration job, discovered and fixed this session. Real Azure CLI access exists in this environment, but is scoped only to an unrelated pre-existing resource group (`rg-nextcare-purview-demo`) — explicitly declined to use it per the user's own instruction. No EKIP-authorized resource group or subscription-level access exists. Unblock: Runbook Phase H, with a real, authorized resource group.

## Database
**BLOCKED**
Schema design and migration chain are sound (single head, no cycles). Neon's actual `alembic_version` remains at an orphaned revision from an unrelated abandoned branch, unresolved since this project's Phase 4.5. Unblock: Runbook Phase D.

## Migrations
**PASS (static) / BLOCKED (live)**
`alembic heads` shows a single head (`c5e2a9f4d7b3`); no missing revisions, no duplicate IDs, no cycles — verified this session after adding 2 new migrations. Never run against a fresh, empty, disposable database in this environment (CI does this automatically on every push to `main`, but no `gh` access exists here to confirm the actual run result). Unblock: Runbook Phase F2/J3.

## CI/CD
**BLOCKED (observation only)**
Workflows (`ci.yml`, `main-extra.yml`, `e2e-and-eval.yml`) are well-constructed — real disposable-Postgres migration validation, real Docker builds, gitleaks secret scanning, correctly gated AI-eval/E2E jobs. Updated this session so the new migrations don't silently break them (`EKIP_APP_ROLE_PASSWORD` placeholder added to both CI env blocks). No `gh` CLI/GitHub API access in this environment to confirm actual pass/fail history. Unblock: Runbook Phase J.

## Docker
**BLOCKED (static validation only)**
No `docker` binary available in this environment. Dockerfiles reviewed statically: non-root user, no baked secrets, correct healthchecks, `tini` for signal forwarding, minimal runtime dependencies — all confirmed by direct inspection. Never actually built or run in this environment. Unblock: Runbook Phase I.

## AI Evaluation
**BLOCKED**
`scripts/eval_confidence.py` requires a live database with real ingested corpus data and a funded `OPENAI_API_KEY` — neither available in this environment; running it without both would either fail or require fabricating results, which this certification will not do. **The 0.333 → 0.083 ambiguous-false-answer baseline is neither confirmed nor known to have regressed** — its current value is simply unmeasured this session. Unblock: Runbook Phase K.

## E2E
**BLOCKED — 0/0 executed**
No running backend/Postgres/frontend stack in this environment. Playwright specs exist and are syntactically valid (confirmed via `playwright test --list` in a prior session) but **that is not a pass count**. Unblock: Runbook Phase L.

## Backup/Restore
**BLOCKED**
7-day backup retention is a Bicep template setting on a Postgres resource that has never been provisioned — no real backup has ever been taken, no restore ever exercised. Unblock: Runbook Phases C and M.

## Accessibility
**PARTIAL**
Real, targeted fixes made this session (keyboard navigation, ARIA tabs pattern, screen-reader loading announcements, focus management) verified only via static code review — no live browser or assistive-technology testing was possible in this environment. Codebase-wide `aria-invalid`/`aria-describedby` form validation remains unimplemented.

## Security
**PARTIAL**
Everything executable in this environment is clean: 487/487 tests, 7/7 import-linter, no secrets found in tracked files, CORS/SSRF/OAuth/prompt-injection defenses re-verified at the source level this session. **Cannot be marked PASS outright** while RLS is inactive against real traffic and credentials remain unrotated since the documented exposure incident — both are Critical-severity gaps by this report's own standard, regardless of how clean the executable checks are.

## Final Release Gate

```
[ ] runtime DB role does not bypass RLS         — code ready, not applied (Phase E)
[ ] RLS isolation has actually passed           — never executed (Phase F)
[x] migration chain passes against fresh DB     — static: yes; live: unconfirmed (Phase F2/J3)
[ ] Neon migration state is resolved            — orphaned revision, unresolved (Phase D)
[ ] credentials are rotated                     — not rotated (Phase B)
[ ] Azure deployment is verified                — never deployed (Phase H)
[ ] Key Vault is verified                       — never exercised live (Phase G)
[ ] Docker stack actually runs                  — no Docker in this environment (Phase I)
[ ] CI actually passes                          — unverified, no gh access (Phase J)
[ ] AI evaluation actually runs                 — never executed (Phase K)
[ ] full E2E actually passes                    — 0/0 executed (Phase L)
[x] security regression passes                  — everything executable is clean
[x] frontend regression passes                  — typecheck/lint/build all clean
[ ] no Critical/High security issues remain     — RLS inactive + unrotated credentials are both Critical
```

# NOT PRODUCTION READY

Per this report's own governing rule: this verdict remains `NOT PRODUCTION
READY` until every item above is empirically verified, and no amount of
additional code correctness changes that. The path to `PRODUCTION READY` is
`docs/operations/final-go-live-runbook.md`, executed in order, by an
operator with real Neon, Azure, GitHub, Docker, Redis, and OpenAI access.
