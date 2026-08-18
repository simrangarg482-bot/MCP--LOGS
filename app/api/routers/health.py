"""Liveness/readiness endpoints (Phase 3 production-readiness pass).

Owned by: app/api -- pure infrastructure/transport concern with no
core/agents business logic to translate, the same category as CORS
middleware in `app.api.main`, not something that belongs behind a
`core.observability` service call.

Deliberately unauthenticated (no `CurrentIdentity` dependency): a load
balancer/orchestrator's health probe has no user session and must never be
made to acquire one just to ask "is this instance alive/ready."

Two distinct questions, not one endpoint doing both (PROJECT_PLAN.md's own
distinction):
  - `GET /health` -- "is the process alive?" No dependency calls at all;
    if this handler runs, the event loop is responsive and the answer is
    always 200. Never used to gate traffic routing, only container restarts.
  - `GET /ready` -- "can this instance safely receive traffic?" Checks the
    one dependency genuinely required for *most* operations (PostgreSQL --
    a lightweight `SELECT 1`, not a real query). Deliberately does NOT
    require Redis: `app.api.main._lifespan`'s own docstring already
    establishes that a Redis outage degrades only the ~1% of endpoints that
    enqueue ingestion jobs (a clean 503 from `get_arq_pool` for those
    specifically), not the whole API -- so readiness reports Redis as an
    informational `degraded` detail rather than failing the whole probe over
    a dependency most traffic never touches. Never calls OpenAI or performs
    an ingestion operation -- both are far too slow/expensive for a probe an
    orchestrator may call every few seconds.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.database.session import engine
from app.shared.config.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessDependency(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: ReadinessDependency
    redis: ReadinessDependency


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness only -- see module docstring. No dependency calls."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    """Readiness -- see module docstring for why Postgres is required but
    Redis is reported, not enforced.
    """
    database = await _check_database()
    redis = await _check_redis(request)

    is_ready = database.status == "ok"
    response.status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        database=database,
        redis=redis,
    )


async def _check_database() -> ReadinessDependency:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return ReadinessDependency(status="ok")
    except Exception as exc:
        logger.warning("readiness_database_check_failed", error=str(exc))
        return ReadinessDependency(status="unavailable", detail="database unreachable")


async def _check_redis(request: Request) -> ReadinessDependency:
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        # Matches `_lifespan`'s own already-established degraded state: a
        # Redis outage at startup (or since) leaves this `None`, and only
        # the specific endpoints that need it return 503 -- not readiness
        # as a whole (see module docstring).
        return ReadinessDependency(
            status="degraded", detail="ingestion sync unavailable; other endpoints unaffected"
        )
    try:
        await arq_pool.ping()
        return ReadinessDependency(status="ok")
    except Exception as exc:
        logger.warning("readiness_redis_check_failed", error=str(exc))
        return ReadinessDependency(
            status="degraded", detail="ingestion sync unavailable; other endpoints unaffected"
        )
