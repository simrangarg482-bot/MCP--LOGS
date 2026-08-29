> **⚠️ THIS GUIDE ITSELF IS NOW STALE — dated 2026-08-06.** It was accurate at the time (every claim below was cross-referenced against real code, not guessed), but the project has moved on substantially since: `docs/PROJECT_STATUS.md` has been kept meticulously up to date through Phase 16 and is now the current source of truth, the opposite of this guide's original warning below. Specific claims in this guide since disproven by later work: "No Docker" (§4.1) — a `Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`, and 3 CI workflows all exist now. "There is no `.env.example`" (§4.3) — `.env.example`/`.env.docker.example` both exist now. `Identity.project_permissions` "populated nowhere" (§3, row 28, and §3.3) — `core/users/service.py`'s `resolve_identity` populates it now. `core.tenancy.service`'s organization/project/SSO/access-rule/invitation functions having "zero REST/MCP surface" (§3.3) — `app/api/routers/tenancy.py`'s `admin_router` now exposes all of them over REST. Treat every other specific claim below the same way: as a snapshot of 2026-08-06, not current fact — verify against the actual code or `docs/PROJECT_STATUS.md` before relying on anything here for testing or onboarding. This guide has not been re-validated end-to-end since; do not assume everything not listed above is still accurate.
>
> **Original 2026-08-06 note, kept for history:** This guide was written by directly reading the EKIP source code — every claim below is cross-referenced to a real file, and every gap is called out explicitly rather than glossed over. Two of the top-level docs in this repo (`docs/PROJECT_STATUS.md`, `docs/PROJECT_STRUCTURE.md`) were stale at the time — they described an early "Phase 1, no implementation yet" state, while the actual `app/` tree was fully built through Milestone 10. That specific staleness in `PROJECT_STATUS.md` has since been fixed; it is now the more current document of the two.

# EKIP User & Developer Testing Guide

---

## Table of contents

1. [What is EKIP?](#1-what-is-ekip)
2. [Project Architecture](#2-project-architecture)
3. [What can this application currently do? (Feature checklist)](#3-what-can-this-application-currently-do)
4. [Prerequisites](#4-prerequisites)
5. [Starting the application](#5-starting-the-application)
6. [REST API Testing (very detailed)](#6-rest-api-testing-very-detailed)
7. [MCP Testing (from zero)](#7-mcp-testing-from-zero)
8. [Real User Walkthrough](#8-real-user-walkthrough)
9. [Testing Checklist](#9-testing-checklist)
10. [Troubleshooting](#10-troubleshooting)
11. [Repository Map](#11-repository-map)

---

## 1. What is EKIP?

### 1.1 The problem

Engineering organizations accumulate knowledge in dozens of disconnected places — Slack threads, GitHub PRs, Jira tickets, Confluence pages, SharePoint docs, Teams channels, and their own past incident postmortems. When something breaks at 2am, the on-call engineer has no single place to ask "has this happened before, and how did we fix it?" EKIP (Enterprise Knowledge & Incident Intelligence Platform) is a backend that ingests all of that scattered knowledge into one searchable, tenant-isolated store, then layers AI agents on top of it that can answer questions, investigate incidents, draft postmortems, and proactively flag gaps in the knowledge base.

### 1.2 Who is the intended user

A multi-tenant SaaS product: each customer is an **organization** (tenant), SSO-provisioned (no username/password signup — see §3 and §10 for exactly what this means in practice). Within an organization, humans use the REST API (or a frontend built on it) to manage incidents, review AI-drafted postmortems, and approve knowledge proposals. AI copilots / LLM clients (Claude Desktop, an IDE agent, or any MCP-compatible tool) use the **MCP server** to ask the same questions and pull the same evidence, on behalf of a real authenticated user.

### 1.3 Major features (implemented, see §3 for exact completeness)

- Multi-tenant org/project/user/role/permission model with Postgres Row-Level Security enforcing tenant isolation at the database layer.
- SSO-only authentication (OIDC — discovery, PKCE, JWKS verification) with JIT (just-in-time) user provisioning on first login.
- Incident lifecycle management (create/update/list/timeline) with audit logging.
- Retrieval-augmented question answering via a LangGraph agent pipeline: Retrieval → Confidence Evaluation → Answer or Investigation routing.
- Incident investigation agent that combines stored evidence with live GitHub/Slack lookups and produces evidence + hypotheses (kept separate on purpose — evidence is verified fact, hypotheses are not).
- Postmortem generation (LLM-drafted, mandatory human-approval gate before anything is "final").
- Knowledge base proposal/review/publish workflow (documents move from `proposed` → `published`, editable only inside that window).
- A daily Knowledge Gap Agent that clusters low-confidence answered questions and recommends new/updated runbooks.
- 8 ingestion connectors: GitHub, Slack, Jira, Teams, Azure DevOps, Confluence, SharePoint, and an internal "runbooks" connector that re-ingests the app's own approved postmortems.
- Hybrid (vector + lexical) retrieval over 3 content collections (documentation, code, conversations), fused via reciprocal rank fusion.
- Envelope-encrypted (AES-256-GCM) storage of connector credentials.
- Per-connector and per-organization ingestion rate limiting.
- An MCP server exposing 6 tools, 2 resources, and 2 prompts, so any MCP-compatible AI client can do everything a REST client can (minus a few REST-only admin operations).
- Structured-logging-based observability dashboards (agent execution stats, MCP tool stats) — no distributed tracing/metrics beyond this yet (see §3).

### 1.4 Technologies involved

FastAPI + Uvicorn (REST), the official `mcp` Python SDK (MCP server, **version 2.0.0** — a major-version jump from what the code was originally written against; already ported, see `app/mcp/servers/server.py`), SQLAlchemy 2.0 (async) + asyncpg + Alembic + PostgreSQL with the `pgvector` extension (all storage, including vectors — no separate vector DB despite a config flag suggesting otherwise, see §3), Redis + `arq` (background job queue), LangGraph + LangChain + `langchain-openai` (agent orchestration + LLM calls), `sentence-transformers` (local, offline embedding + reranking models — no external embedding API), `python-jose` (JWT), `structlog` (logging).

### 1.5 High-level architecture

```mermaid
flowchart TB
    subgraph Clients
        Human["Human via REST client\n(curl / Postman / Swagger UI)"]
        AIClient["AI client via MCP\n(Claude Desktop / Claude Code / any MCP client)"]
    end

    subgraph EKIP["EKIP backend (independent processes)"]
        API["FastAPI REST API\napp/api\nscripts/run_api_server.py"]
        MCP["MCP Server (streamable-HTTP)\napp/mcp\nscripts/run_mcp_server.py"]
        Core["core/ — domain logic\nauth, users, incidents, tenancy, knowledge, audit, observability"]
        Agents["agents/ — LangGraph orchestration\nretrieval -> confidence -> answer/investigation\npostmortem pipeline, knowledge gap pipeline"]
        Retrieval["retrieval/ — hybrid search\npgvector + lexical + RRF fusion"]
        IngestWorker["Ingestion arq worker\napp/ingestion/workers\n(separate process)"]
        AgentsWorker["Agents arq worker\napp/agents/workers\n(separate process, Knowledge Gap cron)"]
    end

    subgraph Data
        PG[("PostgreSQL + pgvector\nall tables incl. vector chunks\nRow-Level Security enforced")]
        Redis[("Redis\narq job queue broker")]
    end

    subgraph External
        Connectors["Connectors\nGitHub / Slack / Jira / Teams /\nAzure DevOps / Confluence / SharePoint"]
        OpenAI["OpenAI API\nAgent LLM calls (gpt-4o-mini)"]
        IdP["Customer's own OIDC IdP\n(Okta / Entra ID / Auth0 / Google Workspace)"]
    end

    Human -->|"Bearer JWT"| API
    AIClient -->|"Bearer JWT (same token as REST)"| MCP
    API --> Core
    MCP --> Core
    API --> Agents
    MCP --> Agents
    Core --> PG
    Agents --> Retrieval
    Agents --> OpenAI
    Retrieval --> PG
    API -.SSO login/callback.-> IdP
    IngestWorker --> Connectors
    IngestWorker --> Retrieval
    IngestWorker --> PG
    IngestWorker <-->|"job queue"| Redis
    AgentsWorker --> Agents
    AgentsWorker <-->|"job queue"| Redis
```

### 1.6 How the pieces cooperate, in one paragraph

A REST or MCP client authenticates with a JWT (the **exact same token** works for both — MCP does not have its own auth mechanism, see §7.4). Both transports are thin: they resolve `Identity`, open a DB session, set a Postgres session variable that Row-Level-Security policies check, then call straight into `core`/`agents` functions — no business logic lives in `app/api` or `app/mcp` themselves. `agents` calls `retrieval` for evidence and OpenAI for generation. Separately, two long-running background worker processes do the asynchronous work: the ingestion worker pulls fresh content from connectors on an hourly cron (plus on-demand), chunks/embeds it, and writes it into the same Postgres/pgvector tables `retrieval` reads from; the agents worker runs a daily Knowledge Gap scan per organization. Everything is tenant-isolated by `organization_id`, enforced both in application code and, as of Milestone 10, by Postgres RLS policies directly on the tables.

---

## 2. Project Architecture

### 2.1 The pieces

| Piece | What it is | Key files |
|---|---|---|
| FastAPI server | REST transport only — no business logic (`app/api/main.py`'s comment: routers are "thin pass-throughs") | `app/api/main.py`, `app/api/deps.py`, `app/api/errors.py`, `app/api/routers/*.py` |
| MCP server | A second, parallel transport to the same `core`/`agents` functions, for AI/LLM clients, over **streamable-HTTP** (not stdio — a deliberate choice, since this is a hosted multi-tenant endpoint, not a per-user local subprocess) | `app/mcp/servers/server.py`, `app/mcp/servers/main.py`, `app/mcp/dispatch.py`, `app/mcp/auth.py`, `app/mcp/tools/*.py`, `app/mcp/resources/*.py`, `app/mcp/prompts/*.py` |
| PostgreSQL | The **only** database — stores everything: relational tables, vector embeddings (via `pgvector`), and enforces tenant isolation via Row-Level Security policies | `app/database/session.py`, `app/database/models/*.py`, `app/database/migrations/versions/*.py` |
| "Vector database" | **Not a separate service.** `pgvector` columns on 3 tables inside the same Postgres instance. A `Settings.default_vector_backend` config flag defaults to `"qdrant"` and `qdrant-client` is even a pinned dependency, but `app/retrieval/qdrant/` is an empty placeholder package and `retrieval/service.py` hardcodes the pgvector store regardless of that setting — **this setting currently does nothing** | `app/retrieval/pgvector/store.py`, `app/retrieval/qdrant/__init__.py` (empty) |
| Retrieval pipeline | Hybrid search: dense (embedding similarity) + lexical (Postgres full-text search) over 3 collections (`documentation_chunks`, `code_chunks`, `conversations_chunks`), merged via reciprocal rank fusion; a cross-encoder reranks results afterward | `app/retrieval/service.py`, `app/retrieval/embedding.py`, `app/retrieval/ranking/fusion.py`, `app/agents/retrieval/reranking.py` |
| Agents | LangGraph state machines: main Ask graph (Retrieval → Confidence → Answer/Investigation), a separate Investigation graph, a linear Postmortem pipeline, a linear Knowledge Gap pipeline | `app/agents/graph.py`, `app/agents/service.py`, `app/agents/confidence.py`, `app/agents/answer/`, `app/agents/investigation/`, `app/agents/postmortem/`, `app/agents/knowledge_gap/` |
| Connectors | 8 source-specific fetchers behind one `Connector` protocol | `app/ingestion/connectors/{github,slack,jira,teams,azure_devops,confluence,sharepoint,runbooks}.py` |
| Authentication | SSO-only OIDC (discovery + PKCE + JWKS verification) issuing EKIP's own JWT access/refresh tokens; MCP reuses the same JWT | `app/core/auth/service.py`, `app/api/routers/auth.py`, `app/mcp/auth.py` |
| Authorization | Role → permission-code model, checked per-operation inside `core` service functions (`require_permission`), not centrally in the auth dependency | `app/core/users/service.py`, `app/database/models/tenancy_models.py` (roles/permissions tables) |
| Background jobs | Two independent `arq` worker processes (ingestion, agents), each with their own cron schedule, backed by Redis | `app/ingestion/workers/{main,tasks}.py`, `app/agents/workers/{main,tasks}.py` |
| External services | OpenAI (agent LLM generation only — embeddings are local), each connector's own API (GitHub/Slack/Jira/etc.), and the customer's own OIDC IdP | `app/agents/llm.py`, `app/shared/config/settings.py` |

### 2.2 Request flow — REST call

```mermaid
sequenceDiagram
    participant C as REST client
    participant API as FastAPI router
    participant Deps as api/deps.py
    participant Auth as core.auth.service
    participant Users as core.users.service
    participant DB as Postgres (RLS-enabled)
    participant Svc as core/agents service
    C->>API: HTTP request + Authorization: Bearer <JWT>
    API->>Deps: Depends(get_current_identity)
    Deps->>Auth: verify_access_token(token)
    Auth-->>Deps: TokenClaims (user_id, organization_id)
    Deps->>Users: resolve_identity(session, user_id, organization_id)
    Users->>DB: SELECT set_config('app.current_organization_id', ...)
    Users->>DB: SELECT roles/permissions (RLS-protected, now visible)
    Users-->>Deps: Identity
    Deps->>DB: set_tenant_context(session, org_id)  (redundant, cheap, belt-and-suspenders)
    Deps-->>API: Identity
    API->>Svc: business call (session, actor, ...)
    Svc->>DB: RLS-scoped reads/writes
    Svc-->>API: Pydantic response model
    API-->>C: JSON response
```

### 2.3 Request flow — MCP tool call

```mermaid
sequenceDiagram
    participant C as MCP client
    participant Tool as mcp/tools/*.py handler
    participant Dispatch as mcp.dispatch.run_mcp_tool
    participant Auth as mcp.auth.resolve_mcp_identity
    participant DB as Postgres (RLS-enabled)
    participant Svc as core/agents service
    participant Log as core.observability.service
    C->>Tool: tools/call (HTTP, Authorization: Bearer <JWT>)
    Tool->>Tool: extract_bearer_token(ctx) reads ctx.headers
    Tool->>Dispatch: run_mcp_tool(tool_name, raw_token, handler)
    Dispatch->>Auth: resolve_mcp_identity(session, raw_token)
    Auth->>Auth: verify_access_token (same fn as REST) + resolve_identity (same fn as REST)
    Dispatch->>DB: set_tenant_context(session, org_id)
    Dispatch->>Svc: handler(session, identity) -> one core/agents call
    Svc->>DB: RLS-scoped reads/writes
    Dispatch-->>Tool: result
    Dispatch->>Log: record_mcp_request(...) [separate session, always runs, in finally]
    Tool-->>C: JSON result
```

### 2.4 Request flow — ingestion job

```mermaid
sequenceDiagram
    participant Cron as arq cron (hourly)
    participant Task as ingestion.workers.tasks
    participant Svc as ingestion.service
    participant Conn as Connector (e.g. github.py)
    participant Retr as retrieval.service
    participant DB as Postgres
    Cron->>Task: scheduled_reconciliation()
    Task->>DB: list_active_connector_config_ids() (cross-tenant, RLS-bypass fn)
    Task->>Task: enqueue run_ingestion_job_task per connector_config_id
    Task->>Svc: run_ingestion_job(session, connector_config_id)
    Svc->>DB: resolve organization_id (RLS-bypass fn) + set_tenant_context
    Svc->>DB: decrypt_secret(credential_ref) [envelope decryption]
    Svc->>Conn: authenticate() + fetch_batch() (rate-limited, 2 token buckets)
    Conn-->>Svc: RawDocuments
    Svc->>Svc: clean -> classify_content_type -> chunk_document
    Svc->>DB: insert_document / insert_document_metadata (inside a savepoint)
    Svc->>Retr: upsert(chunks) [embeds + stores vectors]
    Svc->>DB: update ingestion_jobs + connector sync status
```

### 2.5 Request flow — scheduled agent (Knowledge Gap)

```mermaid
sequenceDiagram
    participant Cron as arq cron (daily 02:00)
    participant Task as agents.workers.tasks
    participant Svc as agents.service.detect_knowledge_gaps
    participant Pipe as agents.knowledge_gap.pipeline
    participant DB as Postgres
    Cron->>Task: scheduled_knowledge_gap_scan()
    Task->>DB: list_organizations() (cross-tenant)
    Task->>Task: enqueue run_knowledge_gap_detection_task per org
    Task->>Svc: detect_knowledge_gaps(session, actor=Identity.for_agent(...))
    Svc->>DB: set_tenant_context + record agent_executions row
    Svc->>Pipe: cluster low-confidence answer_question executions (last 14 days)
    Pipe-->>Svc: GapReport list
    Svc->>DB: persist knowledge_gap_reports
```

---

## 3. What can this application currently do?

Legend: ✅ complete and reachable · 🟡 implemented but with disclosed gaps · ⚠️ implemented but **unreachable** (no REST/MCP surface, or dead/unwired code) · ❌ stub/placeholder only.

### 3.1 Feature checklist

| # | Feature | Status | Exposed via | Implementing files | Expected output |
|---|---|---|---|---|---|
| 1 | SSO login (OIDC discovery + PKCE) | 🟡 | `GET /auth/{org_slug}/login` | `app/core/auth/service.py` (`begin_sso_login`), `app/api/routers/auth.py` | `{authorization_url, state, code_verifier}`; **never run against a live IdP** (module docstring admits this) |
| 2 | SSO callback / JIT provisioning | 🟡 | `POST /auth/callback` | `core/auth/service.py` (`complete_sso_login`), `core/tenancy/service.py` (`evaluate_provisioning`) | `SessionTokens` (access+refresh JWT); group-claim matching only understands Entra ID/Okta shape, not Auth0/Google Workspace |
| 3 | Refresh token rotation + reuse detection | ✅ | `POST /auth/refresh` | `core/auth/service.py::refresh` | New `SessionTokens`; reused-token attempt revokes the whole token family |
| 4 | Logout (single session) | ✅ | `POST /auth/logout` | `core/auth/service.py::logout` | 204, idempotent |
| 5 | Logout everywhere | ⚠️ | none — `revoke_all_sessions` exists in `core/auth/service.py` but has **no REST/MCP endpoint** | `core/auth/service.py` | n/a — unreachable except via direct Python call |
| 6 | Get own profile | ✅ | `GET /auth/me` | `core/users/service.py::get_user_profile` | `UserProfile` with roles/permissions |
| 7 | SSO client-secret resolution | ❌ | internal, used by #1/#2 | `core/auth/service.py::_resolve_client_secret` (line ~239) | **Returns the stored ref as plaintext, unchanged** — explicitly flagged "NOT YET SECURE" in the docstring |
| 8 | Create incident | ✅ | `POST /incidents` | `core/incidents/service.py::create_incident` | 201, `Incident`, writes `audit_logs` |
| 9 | Get / list / update incident, timeline | ✅ | `GET/PATCH /incidents/{id}`, `GET /incidents`, `GET/POST /incidents/{id}/timeline` | `core/incidents/service.py` | See §6.4 |
| 10 | Ask a question (RAG) | ✅ | `POST /ask`, MCP tool `ask_question` | `agents/service.py::answer_question`, `agents/graph.py` | `AskResponse` — never a raw 500 for ordinary failures; unexpected errors become a 200 with `confidence=0.0` |
| 11 | Investigate an incident directly | ✅ | `POST /incidents/{id}/investigate`, MCP tool `investigate_incident` | `agents/service.py::triage_incident` | `AskResponse` with `investigation` populated |
| 12 | Generate + persist postmortem | ✅ | `POST /incidents/{id}/postmortem`, MCP tool `generate_postmortem` | `core/incidents/service.py::trigger_postmortem_generation`, `agents/postmortem/pipeline.py` | 201, `Postmortem` (`status="draft"`) — requires incident already `resolved`/`closed` |
| 13 | Edit / approve postmortem | ✅ | `GET/PATCH /postmortems/{id}`, `POST /postmortems/{id}/approve` | `core/incidents/service.py` | Mandatory human-approval gate before "final" |
| 14 | Propose knowledge document | ⚠️ (REST) / ✅ (MCP) | **MCP tool `propose_runbook_update` only** — `core.knowledge.service.propose_document` has no REST route by design | `core/knowledge/service.py::propose_document` | Creates `documents` row, `status="proposed"` |
| 15 | List proposed documents / publish / reject | ✅ | `GET /knowledge/proposed`, `POST /knowledge/{id}/publish`, `POST /knowledge/{id}/reject` | `core/knowledge/service.py` | Publish also embeds the doc into retrieval in the same transaction |
| 16 | Get one document | ✅ (MCP only) | MCP resource `document://{document_id}` | `core/knowledge/service.py::get_document` | Published docs = org-readable; proposed docs need `knowledge:review` |
| 17 | List knowledge gap reports | ✅ | `GET /knowledge/gaps` | `agents/service.py::list_gap_reports` | `list[GapReport]` |
| 18 | Knowledge Gap detection (agent) | ✅ | Daily cron only (no on-demand trigger endpoint) | `app/agents/workers/tasks.py`, `agents/knowledge_gap/pipeline.py` | New `knowledge_gap_reports` rows |
| 19 | Search similar incidents / recent changes | 🟡 | MCP tools `search_similar_incidents`, `search_recent_changes` (no REST equivalent found) | `agents/service.py` | Read-only search results; "similar incidents" actually searches all collections — no dedicated "incidents" collection exists |
| 20 | Register / list connector configs | ✅ | `POST/GET /tenancy/connectors` | `core/tenancy/service.py::register_connector/list_connectors` | Envelope-encrypts submitted plaintext credential immediately |
| 21 | Create organization / project, SSO config, access rules, invitations | ⚠️ | **No REST or MCP endpoint for any of these** — fully implemented in `core/tenancy/service.py` but only callable from a Python shell/script | `core/tenancy/service.py` | n/a from a client's perspective |
| 22 | Send invitation email | ❌ | n/a | `core/tenancy/service.py::create_invitation` | **Only writes a DB row + audit log — no email is ever sent.** Zero SMTP/SendGrid/etc. code exists anywhere in the repo |
| 23 | Observability dashboards | ✅ | `GET /observability/agents`, `GET /observability/mcp` | `core/observability/service.py` | Note: `/observability/mcp` is intentionally **platform-wide**, not org-scoped, unlike every other endpoint |
| 24 | GitHub / Slack / Jira / Teams / Azure DevOps / Confluence / SharePoint / Runbooks ingestion | 🟡 each | Background worker only (no manual "sync now" endpoint) | `app/ingestion/connectors/*.py` | See §3.2 for per-connector gaps |
| 25 | Connector credential encryption | ✅ | internal | `app/shared/security/{kms,envelope}.py` | Real AES-256-GCM envelope encryption, fresh DEK per secret |
| 26 | Ingestion rate limiting | 🟡 | internal | `app/ingestion/rate_limiter.py` | Two token buckets (per-connector, per-org); **in-process only — not distributed across multiple worker processes** |
| 27 | Postgres Row-Level Security | 🟡 | internal (DB-layer) | `app/database/migrations/versions/c7d4e8f19a2b_*.py`, `d2e5f8a3c1b6_*.py` | **Never run against a live database** — unit-tested with fakes only; the security review's #1 open recommendation is to confirm the app's DB role isn't a table-owner (which would silently bypass RLS entirely) |
| 28 | Project-scoped permission overrides | ✅ (as of a later phase; ❌ at original 2026-08-06 writing) | internal | `core/users/service.py`'s `resolve_identity` populates `Identity.project_permissions` via `repository.get_project_permission_map` | Falls back to org-level permissions only for a project with no explicit override, or for identities built outside `resolve_identity` (e.g. `Identity.for_agent`) |
| 29 | Live evidence augmentation (GitHub/Slack) during investigation | ✅ | internal, used by `investigate_incident` | `app/agents/investigation/live/{github_live,slack_live}.py` | Adds live-fetched evidence to investigation results |
| 30 | Live evidence augmentation (monitoring/metrics) | ⚠️ dead code | n/a | `app/agents/investigation/live/monitoring_live.py` | Fully implemented class, **never registered** in `_LIVE_SOURCES` — cannot be invoked by any real code path |
| 31 | Qdrant vector backend | ❌ dead config | n/a | `app/retrieval/qdrant/` (empty package) | `Settings.default_vector_backend` defaults to `"qdrant"` but is never read — pgvector always used |

### 3.2 Per-connector completeness detail

| Connector | Auth mechanism | Biggest disclosed gap |
|---|---|---|
| GitHub | Bearer PAT / App token | Sequential HTTP calls (no bounded concurrency); files >~1MB skipped |
| Slack | Bot token (`xoxb-`) | No reactions/files/permalinks — `source_url` always `None` |
| Jira | HTTP Basic (`email:api_token`) | **Comments are not fetched at all** in this first pass |
| Teams | Pre-issued Graph OAuth2 token (this connector does not perform the OAuth flow itself) | "Incremental" sync still walks full channel history — Graph has no server-side `since` filter here |
| Azure DevOps | HTTP Basic (empty username + PAT) | Smallest gap of the eight — `since` is a real server-side WIQL filter |
| Confluence | HTTP Basic (`email:api_token`) | Only **pages** — no blogs/comments/attachments |
| SharePoint | Pre-issued Graph OAuth2 token | Only `.txt`/`.md`/`.markdown` files ingested (no Office/PDF parsing); delta-sync token never persisted, so every sync re-walks from scratch |
| Runbooks (internal) | None — internal data | Re-ingests the app's own approved postmortems; no external credential involved |

### 3.3 Dead / unreachable code, precisely

- `app/agents/investigation/live/monitoring_live.py::MonitoringLiveSource` — fully coded, never wired into `_LIVE_SOURCES`, no `ConnectorSource` value exists for it. Cannot run.
- `app/retrieval/qdrant/` — empty placeholder package; `Settings.default_vector_backend` setting has zero effect regardless of value. **Still true** as of this guide's later staleness pass — confirmed still empty.
- ~~`Identity.project_permissions` — accepted as a parameter everywhere, populated nowhere.~~ **No longer true** — see row 28 above; fixed in a later phase, this guide just wasn't updated until now.
- `core.auth.service.revoke_all_sessions` — implemented, zero REST/MCP callers. (Not re-verified in the later staleness pass — treat as unconfirmed, not as still-true.)
- ~~`core.tenancy.service.{create_organization, get_organization, list_organizations, configure_sso, list_projects, create_project, create_access_rule, list_access_rules, deactivate_access_rule, create_invitation, list_invitations, revoke_invitation, accept_invitation}` — fully implemented, zero REST/MCP surface.~~ **No longer true** — `app/api/routers/tenancy.py`'s `admin_router` now exposes all of these over REST (organizations, projects, members, audit, SSO, access rules, invitations), gated by `tenancy:manage` per `core/tenancy/service.py`'s own module docstring. `accept_invitation` itself is still only called internally (during SSO callback and by `accept_invitation_with_password`); the REST-facing invitation-acceptance endpoint calls the latter, not this function directly.

### 3.4 Stale documentation to distrust

**As of this guide's original 2026-08-06 writing:** `docs/PROJECT_STATUS.md` and `docs/PROJECT_STRUCTURE.md` described the project as barely started — that was stale then. `EKIP_STRATEGIC_ANALYSIS.md` claimed `shared/security` is a pass-through stub — false even then; it's real AES-256-GCM encryption; only SSO client-secret resolution (a different function) was still a stub. Several connector docstrings said credential decryption "is not yet built" — also false as of Milestone 10.

**Reversal since then:** `docs/PROJECT_STATUS.md` is no longer stale — it has been kept current through Phase 16 and is now the most reliable single doc in this repo for "what's actually done." This guide (`USER_TESTING_GUIDE.md`) is the one that has since gone stale — see the banner at the top of this file. The connector-docstring staleness noted above has since been fixed directly in each connector's docstring (they now correctly state `credential_ref` is the already-decrypted plaintext by the time a connector sees it, not a placeholder).

---

## 4. Prerequisites

### 4.1 Runtime requirements

- **Python ≥ 3.11** (`pyproject.toml`: `requires-python = ">=3.11"`).
- **PostgreSQL** with the ability to `CREATE EXTENSION vector` (pgvector) — a Neon Postgres instance is what this project was actually developed against (see `app/database/session.py`'s SSL-related comments); any Postgres 14+ with pgvector installed at the OS level will work for local/self-hosted use.
- **Redis** — required, not optional (`Settings.redis_url` has no default). Backs both `arq` worker queues.
- **No Docker.** There is no `docker-compose.yml`, `Dockerfile`, or `docker/` directory anywhere in this repo. You must provision Postgres and Redis yourself (a local install, a free-tier Neon Postgres + free-tier Redis Cloud, or a manually-run `docker run postgres`/`docker run redis` — the compose file simply doesn't exist to do this for you).
- **An OpenAI API key** — required for every agent LLM call (`gpt-4o-mini` by default). Embeddings are local/offline (`sentence-transformers`), so this key is *not* needed for search itself, only for generation (`ask_question`, postmortems, etc.).

### 4.2 Python environment

```bash
cd EKIP---Enterprise-Knowledge-Incident-Intelligence-Platform
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e .
pip install -e ".[dev]"   # pytest, ruff, mypy, import-linter
```

`pip install -e .` is not optional — without an editable install, every script under `scripts/` fails with `ModuleNotFoundError: No module named 'app'` because Python only adds a directly-executed script's own directory to `sys.path`, not the project root.

### 4.3 Environment variables

There is **no `.env.example`** in this repo — the only source of truth is `app/shared/config/settings.py`'s `Settings` class. Create your own `.env` at the repo root:

```dotenv
ENVIRONMENT=development
LOG_LEVEL=INFO

# Required — no defaults
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/ekip
REDIS_URL=redis://user:password@host:6379/0
OPENAI_API_KEY=sk-...
JWT_SECRET_KEY=<a long random string>
CONNECTOR_SECRET_MASTER_KEY=<64 hex characters = 32 raw bytes, e.g. `python -c "import secrets; print(secrets.token_hex(32))"`>

# Optional — shown with their defaults
AGENT_LLM_MODEL=gpt-4o-mini
JWT_ALGORITHM=HS256
JWT_EXPIRY_MINUTES=60
CONFIDENCE_THRESHOLD=0.6
INVESTIGATION_LIVE_EVIDENCE_ENABLED=true
INVESTIGATION_LIVE_EVIDENCE_LOOKBACK_HOURS=24
KNOWLEDGE_GAP_LOOKBACK_DAYS=14
KNOWLEDGE_GAP_MIN_CLUSTER_SIZE=3
KNOWLEDGE_GAP_SIMILARITY_THRESHOLD=0.82
INGESTION_ORG_MAX_REQUESTS_PER_SECOND=5.0

# Declared but currently has NO effect anywhere (pgvector is hardcoded regardless) — leave alone
DEFAULT_VECTOR_BACKEND=qdrant
QDRANT_URL=
```

Never commit this file — it's already `.gitignore`d. Individual connector credentials (Slack tokens, GitHub PATs, etc.) are **not** environment variables — they belong per-organization in `connector_configs` rows, submitted via `POST /tenancy/connectors` (see §6.9).

### 4.4 Database migrations

```bash
alembic upgrade head
```

This applies 5 migrations in order, ending at `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py` (the current head) — including `CREATE EXTENSION IF NOT EXISTS vector;` and every RLS policy. No manual pgvector extension step is needed if your Postgres role has permission to install extensions.

### 4.5 Seed data (mandatory for local testing — read this before §6)

**This app has no username/password login at all.** The only way in is a real SSO round-trip against a real IdP (see §10.7) — completely disproportionate for local testing. So a bypass script exists:

```bash
python scripts/seed_test_organization.py
```

Idempotent — safe to re-run. It creates (or reuses): an org (`Test Org`, slug `test-org`), 6 permission codes, an "admin" role granting all of them, a test user (`student@test-org.example`), and mints a real JWT access token directly (bypassing the entire IdP flow, using the exact same signing function a genuine login would use). It prints the access token, its expiry, the organization ID, and the user ID to stdout — **copy the access token**, you'll need it for every authenticated call in §6 and §7. The token expires after `JWT_EXPIRY_MINUTES` (default 60); just re-run the script for a fresh one.

---

## 5. Starting the application

A full local deployment needs **6 things running simultaneously**: Postgres, Redis (external, not scripted), plus 4 of this repo's own processes. Open 4 separate terminals (all with the venv activated and `.env` loadable).

| # | Process | Command | Required for |
|---|---|---|---|
| 1 | REST API | `python scripts/run_api_server.py` | Everything in §6 |
| 2 | MCP server | `python scripts/run_mcp_server.py` | Everything in §7 |
| 3 | Ingestion worker | `arq app.ingestion.workers.main.WorkerSettings` | Connector syncs (§8) |
| 4 | Agents worker | `arq app.agents.workers.main.WorkerSettings` | Daily Knowledge Gap scan |

**⚠️ Port collision you WILL hit**: the REST API defaults to `0.0.0.0:8000` (hardcoded in `scripts/run_api_server.py`) and the MCP server also defaults to `127.0.0.1:8000` (the installed `mcp` package's own default). Running both unmodified will fail — whichever starts second can't bind. Fix by editing `scripts/run_mcp_server.py`'s last line to pass an explicit port:
```python
server_module.mcp_server.run(transport="streamable-http", port=8001)
```

### 5.1 What success looks like

**REST API** (`python scripts/run_api_server.py`):
```
INFO:     Started server process [xxxx]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```
Then open `http://127.0.0.1:8000/docs` in a browser — Swagger UI should load.

**MCP server** (`python scripts/run_mcp_server.py`):
```
INFO:     Started server process [xxxx]
INFO:     Waiting for application startup.
2026-08-06T... [info] StreamableHTTP session manager started [mcp.server.streamable_http_manager]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8001 (Press CTRL+C to quit)
```
**This is normal, not a bug**: hitting `/`, `/docs`, or `/favicon.ico` in a browser returns 404 — this server only understands the MCP protocol, mounted at `/mcp`. There is no Swagger-like UI for it (see §7).

**Ingestion worker**:
```
arq will run against Redis at ...
Starting worker for 1 functions: run_ingestion_job_task
cron:scheduled_reconciliation ...
```

**Agents worker**: same shape, `functions: run_knowledge_gap_detection_task`, `cron:scheduled_knowledge_gap_scan` scheduled for `hour=2, minute=0`.

### 5.2 Common startup errors

| Error | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | You skipped `pip install -e .`, or you ran the script bare on Windows PowerShell (`scripts/run_api_server.py` instead of `python scripts/run_api_server.py` — PowerShell's `.py` file association silently does nothing) |
| `TypeError: option values must be strings` (alembic) | A `PostgresDsn` object was passed instead of `str(...)` — already fixed in this codebase's `app/database/migrations/base.py`; if you see this again you've reverted that fix |
| `TimeoutError` connecting to Postgres | Genuine network issue, not a code bug — run `python scripts/diagnose_db_connection.py` for a plain-language diagnosis; check the DB host is reachable and (if using Neon) that the compute isn't suspended (cold-start) |
| `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` | Stale — this repo's installed `mcp` package is 2.0.0 (`FastMCP` renamed to `MCPServer`, moved to `mcp.server.mcpserver`); already fixed in this codebase as of this guide's writing |
| MCP server "does nothing" when run bare (`scripts/run_mcp_server.py` with no output) | Same PowerShell `.py`-association issue as above — prefix with `python` |
| Both servers fail to bind on startup | Port 8000 collision between REST API and MCP server — give one an explicit different port (see §5 above) |

---

## 6. REST API Testing (very detailed)

All examples assume the REST API is running at `http://127.0.0.1:8000`.

### Step 1 — Start the server
```bash
python scripts/run_api_server.py
```

### Step 2 — Open Swagger UI
Browse to `http://127.0.0.1:8000/docs`. You'll see 7 route groups: `auth`, `incidents`, `ask`, `postmortems`, `knowledge`, `observability`, `tenancy`.

### Step 3 — Get a JWT (skip real SSO login for now)
```bash
python scripts/seed_test_organization.py
```
Copy the printed `access_token`.

### Step 4 — Authorize Swagger UI
Click the padlock ("Authorize") button, paste `Bearer <access_token>` (or just the token — Swagger's `HTTPBearer` scheme prepends `Bearer` for you) into the dialog, click Authorize. This uses the `HTTPBearer` security scheme added specifically to make this button work (`app/api/deps.py`) — the actual auth logic still reads the raw `Authorization` header underneath.

### Step 5 — Confirm you're authenticated
```bash
curl http://127.0.0.1:8000/auth/me -H "Authorization: Bearer <access_token>"
```
Expected: 200, your `UserProfile` — `email: student@test-org.example`, `roles: ["admin"]`, all 6 permissions listed.

### Step 6 — Create an incident
```bash
curl -X POST http://127.0.0.1:8000/incidents \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"API returning 500s","description":"Checkout endpoint failing intermittently","severity":"high"}'
```
Expected: **201**, an `Incident` object with a new `id`, `status: "open"`. DB change: one new `incidents` row, one `audit_logs` row (`incident.create`).

### Step 7 — List / filter incidents
```bash
curl "http://127.0.0.1:8000/incidents?severity=high&limit=10" -H "Authorization: Bearer <access_token>"
```
Expected: 200, array (note: no `X-Total-Count` header — a disclosed, documented gap in the router itself).

### Step 8 — Update the incident
```bash
curl -X PATCH http://127.0.0.1:8000/incidents/<incident_id> \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"status":"resolved"}'
```
Expected: 200, `resolved_at` auto-stamped (inferred purely from the status transition — there's no `resolved_at` field you can set directly).

### Step 9 — Add a timeline note, then read the timeline
```bash
curl -X POST http://127.0.0.1:8000/incidents/<incident_id>/timeline \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"note":"Rolled back the bad deploy"}'
curl http://127.0.0.1:8000/incidents/<incident_id>/timeline -H "Authorization: Bearer <access_token>"
```

### Step 10 — Ask a question (this is where the LLM gets called — needs a valid `OPENAI_API_KEY`)
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"query":"How do we usually handle checkout 500 errors?"}'
```
Expected: 200, `AskResponse` with `confidence`, `route_taken` (`"answer"` or `"investigation"`), `answer`, `citations`. With an empty knowledge base (nothing ingested yet), expect **low confidence and few/no citations** — this is correct behavior, not a bug; ingest something first (§8) to see grounded answers. DB change: one `agent_executions` row.

### Step 11 — Directly investigate the incident
```bash
curl -X POST http://127.0.0.1:8000/incidents/<incident_id>/investigate -H "Authorization: Bearer <access_token>"
```
Expected: 200, `AskResponse` with `investigation` populated (evidence + hypotheses, kept separate).

### Step 12 — Generate a postmortem (incident must be resolved/closed first — see Step 8)
```bash
curl -X POST http://127.0.0.1:8000/incidents/<incident_id>/postmortem -H "Authorization: Bearer <access_token>"
```
Expected: **201**, `Postmortem` with `status: "draft"`, `generated_by: "agent:postmortem_agent"`. Calling this before resolving the incident returns **409** `postmortem.incident_not_resolved`.

### Step 13 — Edit and approve the postmortem
```bash
curl -X PATCH http://127.0.0.1:8000/postmortems/<postmortem_id> \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"root_cause":"Unhandled null in the payment adapter"}'
curl -X POST http://127.0.0.1:8000/postmortems/<postmortem_id>/approve -H "Authorization: Bearer <access_token>"
```
Expected: second call returns 200, `status: "approved"`, `reviewed_by` = your user id.

### Step 14 — Register a connector (see §8 for realistic per-connector `config` shapes)
```bash
curl -X POST http://127.0.0.1:8000/tenancy/connectors \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"source":"github","credential_ref":"ghp_yourPAT","config":{"repos":[{"repo":"yourorg/yourrepo","ref":"main"}]}}'
```
Expected: **201**, `ConnectorConfig` whose `credential_ref` in the response is now the **encrypted envelope**, not your plaintext PAT (confirm this — if you ever see your raw PAT reflected back, something is badly wrong).
```bash
curl http://127.0.0.1:8000/tenancy/connectors -H "Authorization: Bearer <access_token>"
```

### Step 15 — Review the knowledge queue
Nothing will be here yet unless the ingestion worker has run and produced auto-proposed documents, or you've called the MCP `propose_runbook_update` tool (§7.6) — there is **no REST way to create a proposal**, only to review one:
```bash
curl http://127.0.0.1:8000/knowledge/proposed -H "Authorization: Bearer <access_token>"
curl -X POST http://127.0.0.1:8000/knowledge/<document_id>/publish -H "Authorization: Bearer <access_token>"
```

### Step 16 — Check knowledge gaps
```bash
curl http://127.0.0.1:8000/knowledge/gaps -H "Authorization: Bearer <access_token>"
```
Expect an empty list until the agents worker's daily cron has actually run once against real low-confidence `ask_question` history.

### Step 17 — Observability dashboards
```bash
curl http://127.0.0.1:8000/observability/agents -H "Authorization: Bearer <access_token>"
curl http://127.0.0.1:8000/observability/mcp -H "Authorization: Bearer <access_token>"
```
Remember: `/observability/mcp` shows **every organization's** MCP traffic, not just yours — this is deliberate, not a leak.

### 6.1 Postman notes
Import as a raw collection or just set an environment variable `{{token}}` = your access token, and add header `Authorization: Bearer {{token}}` at the collection level so every request inherits it.

### 6.2 Common errors across all endpoints

| Status | Meaning here |
|---|---|
| 403 `auth.missing_bearer_token` | No `Authorization` header, or it doesn't start with `Bearer ` |
| 403 `auth.invalid_token` | Expired or tampered JWT — re-run the seed script |
| 404 `*.not_found` | Missing row, or belongs to a different organization (same message either way — no cross-tenant enumeration) |
| 409 `*_not_reviewable` / `_not_resolved` / `_already_exists` | State-machine violation — read the specific `error_code` |
| 422 | Pydantic request-body validation failure — this is FastAPI's own default, distinct from the app's domain `ValidationError` (400) |
| 500 | Either a genuinely unexpected exception, or (specifically for `/ask`) — actually **not** possible; `/ask` deliberately converts internal graph failures into a 200 with `confidence=0.0` instead |

---

## 7. MCP Testing (from zero)

### 7.1 What is MCP, and why does this project use it?

MCP (Model Context Protocol) is a standard wire protocol that lets an AI client (a chatbot, an IDE agent, Claude Desktop, etc.) discover and call a server's "tools" (functions), read its "resources" (URI-addressable data), and fetch reusable "prompts" — all in a structured, LLM-friendly way, instead of the AI having to know a bespoke REST API shape. EKIP exposes an MCP server so any MCP-compatible AI client can ask questions, investigate incidents, and propose runbook updates on a real user's behalf, without that client needing custom EKIP-specific integration code.

### 7.2 How MCP differs from REST here

Same backend logic, different transport and different intended caller. REST is for human/scripted clients calling specific known endpoints with typed request/response JSON. MCP is for an LLM that discovers available "tools" dynamically (`tools/list`) and decides which to call based on natural-language intent — the tool's docstring **is** its documentation to the model. Crucially: **both transports share the exact same JWT auth mechanism** — there's no separate MCP login (§7.4).

### Step 1 — Start the MCP server
```bash
python scripts/run_mcp_server.py
```
Expected output — see §5.1. Confirm no port collision with the REST API (§5).

### Step 2 — Get/install an MCP client
Options that work with a streamable-HTTP MCP server: **Claude Desktop** or **Claude Code** (both support adding a remote MCP server by URL), any generic MCP client library (the official `mcp` Python SDK includes client helpers), or hand-crafted JSON-RPC-over-HTTP via curl/Postman (harder — see 7.3 below). **Note:** this repo has **no MCP Inspector / CLI tooling installed** (`mcp>=1.0` is pinned without the `[cli]` extra) and **no example client script** — you're on your own for tooling, this guide can only tell you what the server expects.

### Step 3 — Configure the client
Example Claude Desktop / Claude Code-style remote MCP server config:
```json
{
  "mcpServers": {
    "ekip": {
      "url": "http://127.0.0.1:8001/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer <your JWT access token>"
      }
    }
  }
}
```
Exact config key names vary by client — the two facts that don't vary are: the URL must end in **`/mcp`** (not `/`), and the transport is **streamable-http**.

### Step 4 — Authenticate
There is **no MCP-specific login**. Get a JWT exactly the same way as REST (`python scripts/seed_test_organization.py`, or a real SSO login via `POST /auth/callback`), then send it as a literal HTTP header on every MCP request: `Authorization: Bearer <token>`. Under the hood, `app/mcp/servers/server.py::extract_bearer_token` reads `ctx.headers["authorization"]` and `app/mcp/auth.py::resolve_mcp_identity` calls the **exact same** `verify_access_token` + `resolve_identity` functions the REST layer uses — confirmed by that module's own docstring: "there is only one way to turn a token into an `Identity` in this codebase." Identity is re-resolved on **every single call**, not cached per connection.

### Step 5 — Connect
A compliant client first sends `initialize`, then can call `tools/list` (should return the 6 tools below), `resources/list`/`resources/read` (2 resource templates), and `prompts/list`/`prompts/get` (2 prompts). If your client shows nothing, double check the URL includes `/mcp` and the header is attached — missing/malformed auth surfaces as `PermissionDeniedError` with `error_code: "mcp.missing_token"`.

### Step 6 — Call every tool

| Tool | What it does | Example prompt | Internal flow | DB writes |
|---|---|---|---|---|
| `ask_question(query, incident_id?)` | Confidence-routed RAG answer | "What usually causes checkout 500s?" | → `agents.service.answer_question` → LangGraph (Retrieval→Confidence→Answer/Investigation) | `agent_executions` row |
| `investigate_incident(incident_id)` | Direct investigation, bypasses confidence routing | "Investigate incident abc-123" | → `agents.service.triage_incident` → Investigation graph | `agent_executions` row (only if incident found) |
| `generate_postmortem(incident_id)` | Drafts + **persists** a postmortem | "Draft a postmortem for incident abc-123" | → `core.incidents.service.trigger_postmortem_generation` → postmortem pipeline | New `postmortems` row + `agent_executions` row + audit event |
| `propose_runbook_update(title, content, source_incident_id?)` | Proposes a new KB document for review | "Propose a runbook for handling checkout 500s" | → `core.knowledge.service.propose_document` | New `documents` row (status `proposed`) + metadata rows + audit event |
| `search_recent_changes(query, since?)` | Searches the `code` collection | "What changed recently in the payment service?" | → `agents.service.search_recent_changes` → `retrieval.service.search` | Read-only |
| `search_similar_incidents(description)` | Searches all collections for similar evidence | "Find anything like: checkout returns 500 intermittently" | → `agents.service.search_similar_incidents` | Read-only |

Every call — success or failure — writes exactly one `mcp_requests` row (§ dispatch lifecycle below), separate from any of the above.

### Resources
- `document://{document_id}` → `core.knowledge.service.get_document` — published docs readable by anyone in the org; proposed docs need `knowledge:review`.
- `incident://{incident_id}` → `core.incidents.service.get_incident`.

### Prompts
- `triage-incident(incident_id)` → returns a text template instructing the model to call `investigate_incident` and keep evidence/hypotheses separated.
- `draft-postmortem(incident_id)` → returns a text template instructing the model to call `generate_postmortem`, and warns the result needs human approval via `POST /postmortems/{id}/approve` before being final.

### Step 7 — Connectors through MCP

Connectors are **not** configured through MCP at all — there's no MCP tool for registering a connector. A real user connects GitHub/Slack/Jira/etc. exclusively via the REST endpoint `POST /tenancy/connectors` (§6, Step 14): an admin obtains a PAT/API key/OAuth-issued token **out-of-band** (there is no OAuth redirect flow anywhere in this codebase, for any connector — even the ones that conceptually use OAuth, like Teams/SharePoint, expect the caller to have already completed that dance externally and just hand over the resulting access token as a plain string), then submits it as plaintext JSON to that one REST endpoint. `register_connector` envelope-encrypts it immediately; nothing about this flow is exposed via MCP. Future ingestion jobs decrypt it once per job run (`app/ingestion/service.py`) — the plaintext is never logged or persisted anywhere.

### 7.3 Testing without a full client (raw HTTP)

Only for the technically curious — this repo gives you zero scaffolding for it. You'd need to speak streamable-HTTP's JSON-RPC framing directly: `POST /mcp` with `Content-Type: application/json`, `Accept: application/json, text/event-stream`, body `{"jsonrpc":"2.0","id":1,"method":"initialize",...}`, then subsequent `tools/call` requests, all carrying `Authorization: Bearer <token>`. This is materially harder than REST curl calls — using a real MCP client library is strongly recommended instead.

---

## 8. Real User Walkthrough

**"I just signed up."** In practice today, this means an admin ran `scripts/seed_test_organization.py` (or, in a real deployment, went through the — currently REST/MCP-unreachable — `core.tenancy.service.create_organization` + `configure_sso` + `create_access_rule` flow by hand/script) to stand up your organization and grant you SSO access.

**Now what?** You complete SSO login (`GET /auth/{org_slug}/login` → redirect to your IdP → `POST /auth/callback`) and get a JWT. Locally, skip straight to the seed script's token.

**What should I configure?** Connect at least one connector so there's something to search: `POST /tenancy/connectors` with your GitHub PAT and the repos you care about (§6 Step 14).

**What should I ingest?** Nothing you trigger manually today — ingestion only runs via the hourly cron (`scheduled_reconciliation`) or on connector registration triggering the worker's next pass. Start the ingestion worker (`arq app.ingestion.workers.main.WorkerSettings`) and either wait up to an hour or (for local testing) directly invoke `ingestion.service.run_ingestion_job` from a Python shell for your new `connector_config_id` if you don't want to wait.

**How do I ask questions?** `POST /ask` (REST) or the `ask_question` MCP tool — identical underlying behavior either way, once you've ingested something real, expect actual citations instead of a low-confidence generic answer.

**How do incidents get created?** Manually via `POST /incidents` today — there's no automatic incident-creation-from-alert integration in this codebase (monitoring/alerting live evidence exists as dead code, see §3.3, but nothing feeds it).

**How do agents work?** Reporting an incident and asking about it triggers the Ask graph; explicitly calling `/incidents/{id}/investigate` always goes straight to investigation; resolving an incident and calling `/incidents/{id}/postmortem` drafts a postmortem you must then approve.

**How do I retrieve knowledge?** Either ask a question (agent-mediated, cited) or (if you have `knowledge:review`) browse `GET /knowledge/proposed` for anything auto-proposed and publish it so it's searchable org-wide.

**How do I use MCP?** Same JWT, point your MCP client at `/mcp`, call the same 6 tools — most useful if your actual client is an AI assistant rather than a person clicking Swagger.

---

## 9. Testing Checklist

- [ ] Postgres reachable, `alembic upgrade head` succeeds
- [ ] Redis reachable
- [ ] `scripts/seed_test_organization.py` prints a token
- [ ] REST API starts, `/docs` loads
- [ ] Swagger "Authorize" accepts the token, `/auth/me` returns 200
- [ ] `POST /incidents` writes a DB row
- [ ] `PATCH /incidents/{id}` to `resolved` auto-stamps `resolved_at`
- [ ] `POST /ask` returns an `AskResponse` (even with low confidence on an empty KB)
- [ ] `POST /incidents/{id}/postmortem` fails with 409 on a non-resolved incident, succeeds after resolving
- [ ] `POST /tenancy/connectors` returns an encrypted `credential_ref`, not your plaintext secret
- [ ] Ingestion worker starts, logs `ingestion_reconciliation_scheduled` on the hour
- [ ] A real ingestion job completes, `GET /knowledge/proposed` or search results reflect it
- [ ] `GET /knowledge/{id}/publish` embeds the doc (subsequent `/ask` can cite it)
- [ ] Agents worker starts, logs its cron schedule
- [ ] MCP server starts on a **different port** than the REST API
- [ ] An MCP client's `tools/list` returns all 6 tools
- [ ] MCP `ask_question` succeeds with the same JWT used for REST
- [ ] Each MCP call produces exactly one `mcp_requests` row (check `/observability/mcp`)
- [ ] `/observability/agents` and `/observability/mcp` both return data after some traffic

---

## 10. Troubleshooting

| Symptom | Cause | Solution |
|---|---|---|
| 401 — you'll actually never see this | This app uses 403, not 401, for all auth failures (`PermissionDeniedError.status_hint = 403`) | If you expected 401, that's just not how this API is built — check for 403 instead |
| 403 `auth.missing_bearer_token` | No/malformed `Authorization` header | Add `Authorization: Bearer <token>` |
| 403 `auth.invalid_token` | Expired/tampered JWT | Re-run the seed script or re-login |
| 403 missing-permission errors (e.g. `knowledge:review`) | Your role lacks that permission code | Use the seed script's admin user (has all 6), or grant the permission to your role |
| 404 on everything for an id you know exists | Either genuinely deleted, or belongs to a **different organization** — RLS/service-layer isolation deliberately returns identical 404s either way | Confirm you're using a token for the right org |
| Database unavailable / `TimeoutError` | Real network issue, not app code | `python scripts/diagnose_db_connection.py`; check Neon isn't cold-started/suspended |
| "Vector DB unavailable" | There is no separate vector DB — if pgvector search fails, it's a Postgres/pgvector-extension problem, not a second service | Confirm `CREATE EXTENSION vector` succeeded via migrations |
| OAuth/SSO failure | SSO has never been run against a live IdP in this codebase — expect rough edges | Also check `_resolve_client_secret` isn't silently mishandling your client secret (§3) |
| Missing environment variables | `Settings` will fail to construct at import time | Check §4.3's full list against your `.env` |
| Connector failures | Wrong `credential_ref`, or the specific connector's disclosed gap (§3.2) | Check ingestion worker logs for `<connector>_authenticate_failed`-style structured events |
| MCP connection issues | Wrong URL (must end `/mcp`), port collision with REST API, or missing/mismatched auth header | Re-check §5 (port) and §7.4 (auth) |
| Tool execution failures on MCP | Same `EKIPError` subclasses as REST, just propagated through `run_mcp_tool` | Check the `mcp_requests` row's `status_code` via `/observability/mcp` |
| JWT issues generally | Wrong `JWT_SECRET_KEY`/`JWT_ALGORITHM` between token issuance and verification, or expired token | Re-run seed script; confirm `.env` hasn't changed between minting and verifying a token |
| Docker issues | N/A — there's no Docker setup in this repo to have issues with | Provision Postgres/Redis yourself |
| Migration issues | Usually a stale/wrong `DATABASE_URL`, or trying to run migrations before `pip install -e .` | Confirm `alembic.ini`'s `script_location` and that `Settings` loads correctly first |

---

## 11. Repository Map

| Folder | Responsibility | Key files | Runtime-critical? |
|---|---|---|---|
| `app/api/` | REST transport — thin routers only | `main.py`, `deps.py`, `errors.py`, `routers/*.py` | Yes, for REST clients |
| `app/mcp/` | MCP transport — thin tools/resources/prompts only | `servers/server.py`, `servers/main.py`, `dispatch.py`, `auth.py`, `tools/`, `resources/`, `prompts/` | Yes, only if MCP clients are used |
| `app/core/` | Domain/business logic + its own tables (auth, users, incidents, tenancy, knowledge, audit, observability) | `*/service.py`, `*/schemas.py`, `exceptions.py` | Yes — imported by both API and MCP |
| `app/agents/` | LangGraph orchestration + LLM calls | `graph.py`, `service.py`, `confidence.py`, `answer/`, `investigation/`, `postmortem/`, `knowledge_gap/`, `workers/` | Yes for in-process graph calls; the `workers/` cron needs its own separate process |
| `app/ingestion/` | Connector fetch → clean/chunk → persist pipeline | `service.py`, `connectors/*.py`, `processors/*.py`, `rate_limiter.py`, `workers/` | Yes, as its own worker process, to populate knowledge |
| `app/retrieval/` | Hybrid vector+lexical search library | `service.py`, `embedding.py`, `pgvector/store.py`, `ranking/fusion.py` | Yes, in-process (no separate service) |
| `app/database/` | Async engine/session, ORM models, Alembic migrations — leaf module, imports nothing else in `app/` | `session.py`, `models/*.py`, `migrations/versions/*.py` | Yes — every process needs it |
| `app/shared/` | Settings, logging, shared schemas, secret envelope-encryption | `config/settings.py`, `config/logging.py`, `schemas/*.py`, `security/{kms,envelope}.py` | Yes — imported everywhere |
| `scripts/` | Process entrypoints + one-off dev utilities | `run_api_server.py`, `run_mcp_server.py`, `seed_test_organization.py`, `diagnose_db_connection.py` | Entrypoints are critical; the rest are dev conveniences |
| `tests/` | pytest suite mirroring `app/`'s structure | — | Not runtime-critical, but should stay green |
| `docs/` | Design/planning docs — **several are stale**, see §3.4 | `ENGINEERING_DECISIONS.md`, `AGENT_WORKFLOWS.md`, `DATABASE_DESIGN.md`, `PROJECT_STATUS.md` (stale), `PROJECT_STRUCTURE.md` (stale) | No — documentation only |
| (repo root) | Top-level review/strategy docs | `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md`, `EKIP_STRATEGIC_ANALYSIS.md`, `EKIP_CODE_READING_ROADMAP.md` | No — documentation only, cross-check claims against code |

---

*This guide reflects the repository state as of 2026-08-06. If you extend the app (add a REST endpoint, a connector, an MCP tool), update the relevant table above rather than letting this guide drift the way `docs/PROJECT_STATUS.md` did.*
