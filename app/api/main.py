"""FastAPI application factory for EKIP's REST API.

Owned by: app/api. Per API_DESIGN.md section 1 and ARCHITECTURE.md section 6:
REST and MCP are thin, parallel wrappers around the same core/agents
Pydantic-typed internal interfaces -- this module's routers contain no
business logic beyond request/response translation, matching MCP's tool
handlers' own "no logic beyond this translation" rule.

Scope of what's wired up here (see each router's own docstring for what's
excluded and why): auth (the real SSO/PKCE flow core/auth actually
implements, superseding API_DESIGN.md's older `/auth/login`
username+password sketch), incidents (full CRUD + timeline), ask
(`answer_question` + `triage_incident`), postmortems (generate/read/edit/
approve), knowledge (the review queue -- list/publish/reject/gaps),
observability (`/observability/agents`, `/observability/mcp` -- the
"dashboards, latency metrics" requirement, PROJECT_PLAN.md section 10,
Milestone 10), tenancy (`/tenancy/connectors` -- registering and listing
connector configurations, the previously-missing REST surface for
`core.tenancy.service.register_connector`/`list_connectors`, closed as a
follow-up after Milestone 10), tenancy's `admin_router` (organizations,
projects, SSO configuration, access rules, invitations -- the rest of
`core.tenancy.service`'s previously-unreachable surface, closed in the same
integration-gaps pass that added project-scoped RBAC and logout-everywhere),
and users (`/users/{user_id}/logout-all` -- the admin-triggered session
revocation counterpart to `/auth/logout-all`).
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.errors import ekip_error_handler
from app.api.routers import (
    ask,
    auth,
    incidents,
    knowledge,
    observability,
    postmortems,
    tenancy,
    users,
)
from app.core.exceptions import EKIPError


def create_app() -> FastAPI:
    app = FastAPI(title="EKIP API", version="0.1.0")

    app.add_exception_handler(EKIPError, ekip_error_handler)

    app.include_router(auth.router)
    app.include_router(incidents.router)
    app.include_router(ask.router)
    app.include_router(postmortems.router)
    app.include_router(knowledge.router)
    app.include_router(observability.router)
    app.include_router(tenancy.router)
    app.include_router(tenancy.admin_router)
    app.include_router(users.router)

    return app


app = create_app()
