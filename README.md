# EKIP — Enterprise Knowledge & Incident Intelligence Platform

A modular-monolith platform that ingests engineering knowledge (GitHub, Jira,
Slack, Confluence, documentation), answers questions against it with
confidence-gated, evidence-grounded AI, and runs incident
investigation/postmortem workflows on top of the same retrieval layer.

Core product principle: **a confidently wrong answer is worse than an honest
"I don't know."** Every AI-generated answer is grounded in cited evidence or
escalates to an investigation rather than fabricating a response.

## Architecture

A modular monolith (not microservices) with enforced module boundaries —
see `docs/Architecture.md` for the full picture and `pyproject.toml`'s
`[tool.importlinter]` contracts for what's mechanically enforced in CI.

```
api/          FastAPI REST layer — thin pass-through, no business logic
core/         Domain services (auth, tenancy, incidents, knowledge, users, audit)
agents/       LangGraph-based AI agents (answer, investigation, postmortem, knowledge-gap)
retrieval/    Hybrid search (vector + lexical), independently replaceable
ingestion/    Connectors (GitHub, Jira, Slack, Confluence, Teams, SharePoint, Azure DevOps)
mcp/          Model Context Protocol server — same core/agents surface for AI clients
database/     SQLAlchemy models + Alembic migrations
shared/       Config, logging, security (KMS/envelope encryption) — cross-cutting only
```

Frontend: React + TypeScript + Vite, under `frontend/`.

## Local development

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22, and a
Postgres connection with the `vector` extension available (a real remote
instance, or the local `docker compose` stack below).

```bash
uv sync --extra dev
cp .env.example .env   # fill in DATABASE_URL, REDIS_URL, OPENAI_API_KEY, etc.
uv run alembic upgrade head
uv run uvicorn app.api.main:app --reload
```

In separate terminals:

```bash
uv run python scripts/run_ingestion_worker.py          # Redis-resilient ingestion worker
cd frontend && npm install && npm run dev              # frontend dev server
```

## Testing

```bash
uv run pytest tests/ -q                # backend (fully mocked — no real DB/Redis needed)
uv run lint-imports                    # module boundary contracts
cd frontend && npm run typecheck && npm run lint && npm run build
cd frontend && npx playwright test     # browser E2E — needs a real running stack
```

See `docs/operations/ci.md` for what runs automatically in CI. Real
connector E2E coverage (`frontend/e2e/critical-workflow.spec.ts`) needs
`EKIP_TEST_GITHUB_TOKEN`/`EKIP_TEST_GITHUB_REPOS` in `.env` — skipped
cleanly if absent.

## Running the full stack in Docker

```bash
cp .env.docker.example .env.docker   # fill in OPENAI_API_KEY
docker compose up --build
```

This starts a self-contained Postgres + Redis + backend + worker + frontend
stack (`docker-compose.yml`). See `docs/operations/local-production.md` for
details, health checks, and troubleshooting.

## Deployment

Designed for Azure (Container Apps, PostgreSQL Flexible Server, Azure Cache
for Redis, Key Vault) via the parameterized Bicep template in `infra/`. See
`docs/operations/deployment.md` for the full procedure and current status —
**Azure infrastructure has not yet been provisioned or deployed**; the
templates and pipeline are ready, gated on the deploying identity having
appropriate (least-privilege) Azure permissions.

For a single-provider path with no external blockers, `docs/operations/
deployment-railway.md` runs the whole stack (Postgres + pgvector, Redis,
backend, both workers, MCP, frontend) on Railway via the committed
`railway.*.json` config files and `.env.railway.example`.

## Documentation

| Doc | Covers |
|---|---|
| `docs/Architecture.md` | System design, module boundaries |
| `docs/PROJECT_PLAN.md` | Feature scope and milestones |
| `docs/DATABASE_DESIGN.md` | Schema, tenant isolation (RLS) |
| `docs/AGENT_WORKFLOWS.md` | Agent graph, confidence gating, grounding |
| `docs/ENGINEERING_DECISIONS.md` | Notable technical decisions and their rationale |
| `docs/operations/local-production.md` | Running the Docker Compose stack |
| `docs/operations/ci.md` | CI pipeline tiers, secret handling |
| `docs/operations/deployment.md` | Deployment procedure, Azure status |
| `docs/operations/deployment-cloudflare.md` | Alternative deployment: Cloudflare Pages + Render, no Docker |
| `docs/operations/deployment-railway.md` | Alternative deployment: full stack on Railway |
| `docs/operations/rollback.md` | Rollback/recovery procedures |
