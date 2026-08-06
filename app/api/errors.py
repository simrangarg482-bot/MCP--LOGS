"""`EKIPError` -> HTTP response mapping for app/api.

Owned by: app/api. A single exception handler registered against the base
`EKIPError` (app/core/exceptions.py) -- every core/agents service function
raises from that hierarchy, and each subclass already carries its own
`status_hint` plus a `.to_error_body()` giving the exact
`{"error_code", "message", "detail"}` wire shape API_DESIGN.md's "Design
conventions" section specifies. Registering one handler here means no
individual router needs its own try/except EKIPError block.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.exceptions import EKIPError


async def ekip_error_handler(_request: Request, exc: EKIPError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_hint, content=exc.to_error_body())
