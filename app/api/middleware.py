"""Request correlation middleware (Phase 5.2).

Owned by: app/api -- pure transport/observability concern, the same
category as CORS middleware in `app.api.main`, not business logic.

Generates (or accepts) one request ID per HTTP request, binds it into
structlog's contextvars for the lifetime of that request -- so every log
line emitted anywhere during that request's handling (router, service,
agent, retrieval, LLM call) carries the same `request_id` automatically,
with no call site needing to thread it through function signatures by hand.
This is what makes "the same logical operation traceable end to end" a
property of the logging configuration, not something every module has to
opt into separately.

Also logs one structured `http_request_completed` event per request with
method/path/status_code/duration_ms -- the minimum viable request-level
metric this phase's observability work builds on (Phase 5.5/5.6's
retrieval/ingestion-specific timing are separate, narrower events; this one
covers the outermost "how long did the whole HTTP request take" question
for every endpoint uniformly, without each router needing its own timer).
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request ID into structlog contextvars for the duration of
    each request, and logs one completion event per request.

    Accepts a caller-supplied `X-Request-ID` (e.g. from an upstream load
    balancer/gateway that already assigns one) rather than always minting
    a fresh one -- this is what makes the id useful for correlating across
    a system this application is only one part of, not just internally.
    Always echoes the id back in the response header, whether it was
    supplied or generated here, so the caller can log/display the same
    value this system used.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or str(uuid.uuid4())

        # Cleared first, not just bound -- structlog's contextvars are
        # asyncio-context-scoped, which lines up with one request = one
        # task under Starlette/uvicorn, but clearing defensively here means
        # a future change to that assumption fails safe (a stale bound
        # value from a prior request, not a silently wrong one carried
        # forward) rather than leaking one request's id into another's logs.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
                request_id=request_id,
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info(
            "http_request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            request_id=request_id,
        )
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response
