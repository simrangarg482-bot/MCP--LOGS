# EKIP — Data Lifecycle, Ownership Map, and Data-Subject Deletion

**No GDPR or other regulatory compliance is claimed by this document or by
the code it describes.** This is a technical deletion mechanism. Whether it
satisfies any particular legal obligation is a determination this project
cannot make, and several inputs to that determination (retention periods,
lawful basis, backup policy, processor agreements) do not exist yet — see
[Limitations](#7-limitations-what-this-does-not-guarantee) and
[Pending decisions](#8-deferred-pending-a-product-or-legal-decision).

Implementation: `app/core/privacy/`. Endpoints: `GET
/users/{user_id}/data-deletion/plan` (dry run), `POST
/users/{user_id}/data-deletion` (execute).

---

## 1. Where personal data actually lives

Verified against the full schema (`app/database/models/*.py`), not assumed.
**Exactly two columns in the entire database hold raw personal data:**

| Table | Column | Note |
|---|---|---|
| `users` | `email`, `display_name` | Plus `password_hash`, a credential |
| `invitations` | `email` | The invitee's address; may predate any `users` row |

Every *other* reference to a person is one of:

- a **surrogate UUID foreign key** (`incidents.reported_by`,
  `postmortems.reviewed_by`, `agent_executions.user_id`, …), or
- a **tagged-actor string** of the form `"user:<uuid>"`
  (`audit_logs.actor`, `incident_timeline.actor`, `postmortems.generated_by`,
  `mcp_requests.identity`).

Both kinds dereference to the `users` row. **This is the single most
important fact in this document**: anonymizing that one row neutralizes every
downstream reference at once, with no need to rewrite `audit_logs` — which is
append-only by explicit contract ("No updates, no deletes, ever", per
`AuditLog`'s own docstring) and must not be rewritten.

Also verified: **all user-attributable data is in Postgres.** There is no
object storage, no S3/Azure Blob, no upload directory, and Redis holds only
`arq` job queues (payloads are organization/connector UUIDs, self-expiring
in ~1h). Office/PDF extraction is entirely in-memory.

---

## 2. Data ownership map

`ondelete` values are the real constraints in the models, and they are the
*evidence* for each action — not a preference. CASCADE marks data the schema
treats as disposable-with-the-user; RESTRICT marks data the schema treats as
history that outlives the person.

### Identity, access, and authentication

| Data | Table | Owner | Scope | `ondelete` (→`users`) | Action |
|---|---|---|---|---|---|
| User account | `users` | The person | Global (no `organization_id`) | — | **Anonymize** |
| Sessions / refresh tokens | `refresh_tokens` | The person | org + user | CASCADE | **Hard delete** |
| Org role assignments | `user_roles` | The person (grant) | org + user | CASCADE | **Hard delete** |
| Project memberships | `project_memberships` | The person (grant) | user + project | CASCADE | **Hard delete** |
| SSO identity links | `external_identity_mappings` | The person | org + user | CASCADE | **Hard delete** |
| Role/permission catalog | `roles`, `permissions`, `role_permissions` | Platform | Global | — | **Retain** (not user data) |

### Organization-owned data — retained on user deletion

| Data | Table | Owner | Evidence it is not user-owned |
|---|---|---|---|
| Organization, projects | `organizations`, `projects` | Organization | No `user_id` column |
| Ingested documents | `documents` | Organization | `organization_id` + `project_id`; **no `user_id` column at all** |
| Chunks + embeddings | `documentation_chunks`, `code_chunks`, `conversations_chunks` | Organization | Derived from `documents`; no `user_id` |
| Document metadata | `document_metadata` | Organization | Child of `documents` |
| Connector config + credentials | `connector_configs` | **Organization** | **No `user_id` column** — see §4 |
| Ingestion job history | `ingestion_jobs` | Organization | `organization_id`; no `user_id` |
| SSO config, access rules | `sso_configurations`, `organization_access_rules` | Organization | No `user_id` |
| Incidents | `incidents` | Organization | `reported_by` is **RESTRICT** |
| Incident timeline | `incident_timeline` | Organization | Actor is a tagged string, not an FK |
| Postmortems | `postmortems` | Organization | `reviewed_by` is **RESTRICT** |
| Knowledge-gap reports | `knowledge_gap_reports` | Organization | `organization_id`; no `user_id` |
| Audit trail | `audit_logs` | Organization | Append-only contract; actor is a tagged string |
| MCP request log | `mcp_requests` | Organization | `identity` is a tagged string; no FK at all |
| OAuth clients | `oauth_clients` | Platform | Deliberately not org- or user-scoped |

### User-attributable telemetry — anonymized, not deleted

| Data | Table | `ondelete` | Action | Why |
|---|---|---|---|---|
| Agent execution / ask history | `agent_executions` | **SET NULL** | **Anonymize** (`user_id` → NULL) | Org-level cost/usage/confidence telemetry that happens to record who triggered it. The FK's own `SET NULL` is the schema declaring this. Deleting rows would rewrite the organization's usage and cost history. |
| Invitations received | `invitations` | `invited_by` is RESTRICT | **Anonymize** (email → placeholder) | Row is partly an audit record of who invited whom, and backs the one-pending-invite-per-email index. Only the address is removed. |

### Why the `users` row is anonymized rather than deleted

Three `ON DELETE RESTRICT` foreign keys point at `users.id`:

```
incidents.reported_by     → RESTRICT
postmortems.reviewed_by   → RESTRICT
invitations.invited_by    → RESTRICT
```

A `DELETE FROM users` therefore **fails** for any user who has ever reported
an incident, reviewed a postmortem, or sent an invitation. This is not an
obstacle to work around — it is the schema stating that this history outlives
the individual. So deletion is implemented as anonymization of that row:

```
email         → deleted-user-<uuid>@deleted.invalid   (RFC 2606 reserved TLD)
display_name  → "Deleted User"
password_hash → NULL      (permanently disables password login)
is_active     → false     (blocks login at the service layer)
```

The placeholder email is *derived from the user id*, which makes it both
unique (`users.email` is `UNIQUE NOT NULL`, so it cannot simply be nulled)
and deterministic — anonymizing twice produces the identical value, which is
part of what makes the operation idempotent.

---

## 3. Deletion workflow

```
POST /users/{id}/data-deletion
        ↓
Authorization      tenancy:manage, via the existing require_permission
        ↓          organization = actor.organization_id (never a parameter)
Discovery          count rows per category — read-only
        ↓
Plan               DeletionPlan: per-category action + count + rationale
        ↓          (GET .../plan returns exactly this, mutating nothing)
Execution          each step independently; failures recorded, not fatal
        ↓
Derived cleanup    (user scope: none needed — no user-owned derived data)
        ↓
Audit              counts + status only, never the deleted personal data
        ↓
Result             completed | partially_completed | failed
```

**Dry run runs the same code.** `execute_user_data_deletion` calls
`plan_user_data_deletion` and acts on its output, so the preview cannot drift
from real behavior.

### Idempotency

Every mutation is a `DELETE/UPDATE … WHERE` that matches zero rows once
applied, so re-running is a successful no-op rather than an error. The result
sets `was_noop=true` when the user record was already anonymized on entry —
the signal that distinguishes "this run did the work" from "a previous run
already had", which a zero row-count alone cannot express. Retry after a
partial failure is the supported recovery path.

### Partial failure

Steps execute independently. A failing step is recorded with its error and
the remaining steps still run, so one broken step cannot silently block the
rest of a person's data being cleaned. Status is then `partially_completed`
or `failed` — **never `completed`**.

One caveat stated plainly: because all steps share the caller's transaction,
a database error that poisons that transaction will cause subsequent steps to
fail too, and the commit itself may fail. The per-step accounting is most
useful for application-level failures; it is *not* a claim that this can
partially commit inside a poisoned transaction.

### Why synchronous, with no job table

User-scoped deletion touches only small, bounded, per-user row sets and
notably does **not** touch documents, chunks, or embeddings (organization-
owned, retained). That is a handful of indexed single-statement writes,
comfortably inside one request and one transaction — and doing it
synchronously buys atomicity, an immediately observable result, and no window
where a "pending" request is visible but unapplied. Organization deletion is
the case that genuinely needs the job model and the `arq` worker; it is
deliberately not implemented (§8).

---

## 4. Connector and knowledge ownership — resolved by schema evidence

The question "if a user configured a connector, do they own what it
ingested?" is answered by the schema, not by guesswork:

```
User ──configured──▶ ConnectorConfig ──ingests──▶ Documents ──▶ Chunks
                     (organization_id)            (organization_id)
                     NO user_id column            NO user_id column
```

`connector_configs` has **no `user_id` column at all**. Neither does
`documents`. A connector is organization property; the person who set it up
is not recorded on it. **Deleting a user therefore does not touch connectors,
credentials, documents, chunks, or embeddings** — and must not, since that
would destroy organization knowledge in response to one employee leaving.

---

## 5. Derived-data cleanup, and a real bug this work fixed

Discovery found a genuine, confirmed data-lifecycle defect:

`core.knowledge.service.reject_document` soft-deletes a document
(`documents.deleted_at`). `core/knowledge`'s own reads filter that column, so
the document correctly disappeared from the review UI. But the derived rows
in the three `*_chunks` tables — each holding its own copy of the text **plus
its embedding** — were never purged, and `retrieval/pgvector/store.py`'s
queries did not filter on `deleted_at` either (they joined `documents` only
for `title`/`source_url`).

**Net effect: a human could reject a document and the Answer Agent would
still retrieve, quote, and cite it.**

Two independent barriers now exist, because either alone leaves a hole:

1. **Query-time exclusion** — both `search()` and `lexical_search()` now add
   `WHERE documents.deleted_at IS NULL`. Both halves of hybrid search need it
   independently: fusing a filtered dense list with an unfiltered lexical one
   would reintroduce the leak. This also protects rows soft-deleted *before*
   this change shipped.
2. **Storage purge** — `reject_document` now purges the chunk rows via the
   existing `retrieval.service.delete` primitive (which existed but had no
   callers), across all three collections, in the same transaction as the soft
   delete. A query filter alone would leave rejected content sitting in
   storage indefinitely.

Tested in `tests/core/privacy/test_derived_data_cleanup.py`.

Note the FK chain already handles the *hard*-delete case:
`documents` → `document_metadata` and all three `*_chunks` tables are
`ON DELETE CASCADE`. The bug existed specifically because the code path uses
soft deletion, where cascades never fire — which is exactly why "foreign key
cascades solve derived-data cleanup" is not a safe assumption.

---

## 6. Data actions summary

**Hard deleted** — `refresh_tokens`, `user_roles`, `project_memberships`,
`external_identity_mappings` (all scoped to the caller's organization).

**Anonymized** — `users` (email/display_name/password_hash cleared,
deactivated), `invitations.email` for that address, `agent_executions.user_id`
→ NULL.

**Retained** — all organization-owned knowledge and history: documents,
chunks, embeddings, connectors, ingestion jobs, incidents, incident
timelines, postmortems, knowledge-gap reports, audit logs, MCP request logs,
and the platform role/permission catalog.

---

## 7. Limitations — what this does **not** guarantee

These are real and verified, not hypothetical:

- **Access tokens remain valid until expiry.** Access tokens are stateless
  JWTs (`core.auth.service.verify_access_token` does no database lookup), so
  an already-issued one keeps working for up to `jwt_expiry_minutes` after
  deletion. Refresh tokens are deleted, so no *new* access token can be
  minted.
- **stdout logs are outside the deletion boundary.** Structured logs go to
  stdout only, and every authenticated request binds `user_id` into its log
  context (`app/api/deps.py`). Two paths log raw email addresses
  (`core.users.service`'s `user_provisioned`, `core.tenancy.service`'s
  `provisioning_denied`). Whatever collects those logs is not reachable by
  this code.
- **In-memory MCP OAuth state** (`EkipOAuthProvider._issued_sessions`,
  ≤300 s TTL, process-local) can briefly hold a deleted user's tokens. The
  underlying `refresh_tokens` row is deleted, so it cannot be exchanged for a
  new session.
- **Rate-limiter keys** embed a user id in an in-process dict
  (`app/shared/rate_limiter.py`). Values are only `(tokens, timestamp)`; the
  state is process-local and wiped on restart. Not cleaned, and judged not to
  need it — documented rather than silently ignored.
- **Backups have their own lifecycle.** Database backups/PITR are outside
  this code's reach entirely. No backup redaction policy exists.
- **External source systems are untouched.** Deleting ingested content does
  not delete anything in GitHub, Slack, Jira, Teams, SharePoint, or Azure
  DevOps.
- **LLM provider-side data is outside our control.** Prompts sent to the
  model provider are subject to that provider's retention, not ours.
- **No retention policy or scheduled purge exists.** Nothing expires
  automatically anywhere in this system: revoked/expired `refresh_tokens`
  accumulate forever, and no cleanup job exists (confirmed — two docstrings
  in `core/tenancy` already flag this). Inventing legal retention periods is
  explicitly out of scope.
- **`incidents.deleted_at` / `postmortems.deleted_at` are inert.** Both
  columns exist but no code reads or writes them. Left untouched: starting to
  use them here would create a half-enforced soft delete.
- **RLS is not the enforcement mechanism here.** Tenant scoping in this
  module is application-level (`organization_id` in every statement). See
  `docs/PROJECT_STATUS.md` for the separate, still-open RLS runtime-role
  item.

---

## 8. Deferred: pending a product or legal decision

Declared in `DeletionScope` so the API vocabulary is stable, and **rejected
at the service boundary with an explicit error** rather than silently doing
something partial:

- **`user_account`** — actually removing the `users` row. Requires deciding
  what should happen to the three RESTRICT references (repoint to a shared
  "deleted user" sentinel row? relax the constraints? accept that the row is
  permanent?). Anonymization already removes the personal data; this is about
  the surrogate key itself.
- **`organization`** — full tenant deletion. Needs a product decision on
  knowledge ownership at offboarding (export first? grace period?), plus the
  job/worker model, since it would cascade across documents, all three chunk
  tables, and every embedding at a scale no HTTP request should hold a
  transaction open for. Note `organizations` is currently blocked by RESTRICT
  from `incidents`, `documents`, `ingestion_jobs`, `audit_logs`, `user_roles`,
  `agent_executions`, `knowledge_gap_reports`, and the chunk tables.
- **Self-service deletion** ("delete my own account"). Currently
  admin-only (`tenancy:manage`). Whether a person may erase their own data
  while org-owned incident history still attributes work to them is a policy
  question.
- **Legal retention enforcement**, **automated deletion scheduling**, and
  **audit export** (the audit *trail* exists; there is no export endpoint).
