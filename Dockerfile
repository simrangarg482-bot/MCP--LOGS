# EKIP backend/worker image (Phase 3 production-readiness pass).
#
# Serves two roles from one image, differing only by CMD (docker-compose.yml
# overrides it for the worker service) -- the API server and the arq worker
# share the exact same dependency set and application code
# (ENGINEERING_DECISIONS.md #002: separate processes, one Redis queue), so
# building/publishing two near-identical images would only add drift risk,
# not safety.
#
# Two-stage build:
#   1. `builder` resolves and installs dependencies via `uv sync --frozen`
#      against the committed `uv.lock` -- deterministic, pinned-to-the-hash
#      installs, not whatever `>=` ranges in pyproject.toml happen to
#      resolve to on build day.
#   2. `runtime` copies only the resulting virtualenv + application code
#      into a fresh, minimal base -- no compiler toolchain, no uv binary,
#      no build cache left in the shipped image.

FROM python:3.13-slim AS builder

# Pinned uv release binary, not `pip install uv` -- avoids needing a
# resolvable uv version from the base image's own Python/pip first.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first (cached across builds whenever only application code
# changes) -- `--no-install-project` installs everything the project depends
# on without needing `app/` present yet.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the real application code, then install the project itself.
# README.md is required, not optional: pyproject.toml's `readme = "README.md"`
# makes the project build (`uv sync` below installs `app/` itself) fail hard
# with "readme file does not exist" without it.
COPY README.md ./
COPY app/ ./app/
COPY alembic.ini ./
# All three process entrypoints ship in this one image -- the API server, the
# ingestion worker, and the MCP server -- selected per service by the start
# command (docker-compose.yml's `worker`, the Railway config files). The rest
# of scripts/ is local eval/live-test tooling and is excluded by .dockerignore.
COPY scripts/run_api_server.py scripts/run_ingestion_worker.py scripts/run_mcp_server.py ./scripts/
RUN uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# tini as PID 1: forwards SIGTERM correctly to uvicorn/arq (neither process
# is designed to run as PID 1 itself) -- see the graceful-shutdown notes in
# docs/operations/deployment.md for what each does with that signal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user -- the application never needs to write anywhere in
# the image itself (all state is Postgres/Redis), so no writable volume is
# granted either.
RUN groupadd --system --gid 1000 ekip \
    && useradd --system --uid 1000 --gid ekip --no-create-home ekip

WORKDIR /app
COPY --from=builder --chown=ekip:ekip /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER ekip

EXPOSE 8000

# Liveness only (`app.api.routers.health.health` -- no dependency checks) --
# a container orchestrator's own readiness probe should hit `/ready`
# separately; Docker's single HEALTHCHECK primitive here answers "is the
# process alive," matching this image's own primary failure mode (a hung
# event loop), not "is Postgres reachable" (which would make Docker restart
# a perfectly healthy container during a transient DB blip).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["tini", "--"]

# Overridden by docker-compose.yml's `worker` service to run the arq worker
# instead (`python scripts/run_ingestion_worker.py`) -- same image,
# same installed dependencies, different process.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
