# Running EKIP locally in Docker

A reproducible, production-shaped local stack (`docker-compose.yml`):
Postgres (with pgvector) + Redis + backend + arq worker + frontend, all
built from this repo's own Dockerfiles — the same images a real deployment
would use, running against fully local, disposable infrastructure.

## Setup

```bash
cp .env.docker.example .env.docker
# edit .env.docker: fill in a real OPENAI_API_KEY
docker compose up --build
```

Everything else in `.env.docker.example` is a safe, local-only placeholder
(a throwaway JWT signing key, a throwaway 32-byte KMS key, `KMS_PROVIDER=local`)
— fine for a disposable local stack, never for a real deployment. See
`docs/operations/deployment.md` for the production secret model.

## What starts, in order

1. `postgres` — `pgvector/pgvector:pg16`, waits for a real health check
   (`pg_isready`) before anything depends on it.
2. `redis` — `redis:7-alpine`, same health-gated pattern.
3. `migrate` — runs `alembic upgrade head` once, then exits. `backend`/
   `worker` both wait for this to **complete successfully**, not just for
   Postgres to be reachable — a deliberate choice (section 22): the
   application container never runs a migration implicitly at startup, so a
   migration failure is diagnosed on its own, not conflated with "the app
   won't start."
4. `backend` — the FastAPI API, port `8000`. Has its own container
   `HEALTHCHECK` (`GET /health`).
5. `worker` — the same backend image, different `command:` (the arq worker
   entrypoint) — see `Dockerfile`'s own comment on why one image serves
   both roles.
6. `frontend` — built with `VITE_API_BASE_URL=http://localhost:8000` baked
   in at build time (Vite compiles it into the bundle — see
   `frontend/Dockerfile`'s own comment on why this can never be a secret),
   served by nginx on port `8080`.

## Verifying it's up

```bash
curl http://localhost:8000/health   # {"status": "ok"} -- liveness only
curl http://localhost:8000/ready    # database + redis dependency status
open http://localhost:8080          # the app itself
```

## Seeding test data

The stack starts with an empty, freshly-migrated database — no
organizations/users exist yet. Either sign up through the UI
(`POST /auth/signup`), or run the deterministic E2E seed script against it:

```bash
DATABASE_URL=postgresql+asyncpg://ekip:ekip_local_dev_only@localhost:5432/ekip \
  uv run python scripts/e2e_seed.py
```

## Known limitations

- This stack was authored and syntax-validated (Dockerfile structure,
  `docker-compose.yml` YAML) in an environment with no Docker daemon
  available — it has not yet been built-and-run end-to-end. The equivalent
  behavior (fresh migrations, seed script, application startup, real
  login) *was* verified for real against a disposable database in the
  project's actual Neon Postgres project (see `docs/operations/deployment.md`'s
  migration-validation section) — the same code paths, just not through
  these exact container images yet. Treat `docker compose up` as
  high-confidence but not yet independently confirmed; the CI workflow
  (`.github/workflows/main-extra.yml`) builds both images on every push to
  `main` and will catch a broken Dockerfile immediately once CI is enabled.
