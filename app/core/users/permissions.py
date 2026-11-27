"""The assignable-permission catalog and the default role templates.

Single source of truth for *which permission codes an organization admin may
put on a custom role* and *what the three starter roles contain*. Everything
else (`ADMIN_PERMISSION_CODES`, the API's `GET .../permissions`, the frontend's
grouped checkboxes) derives from here.

Why a fixed catalog rather than "whatever is in the `permissions` table": the
table also holds codes that must never be handed out by an admin -- e.g.
`system:graph_discovery`, an internal capability used by background jobs
(`core.graph.service`). Anything not listed in `ASSIGNABLE_PERMISSIONS` is
therefore invisible to the role-management API and rejected if submitted.

Adding a permission code: add it to `ASSIGNABLE_PERMISSIONS` here AND write a
migration that inserts the `permissions` row and grants it to the built-in
`admin` role. Built-in roles are immutable to the application role
(`ekip_app`) at the database level, so signup can no longer self-heal a missing
grant -- `tests/core/users/test_permission_catalog.py` fails if this list and
the migration's admin grant list drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionSpec:
    code: str
    label: str
    description: str
    group: str


ASSIGNABLE_PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        "incident:read",
        "View incidents",
        "See incidents, their timelines and related details.",
        "Incidents",
    ),
    PermissionSpec(
        "incident:write",
        "Create and edit incidents",
        "Report new incidents and update, resolve or annotate existing ones.",
        "Incidents",
    ),
    PermissionSpec(
        "postmortem:write",
        "Write postmortems",
        "Draft and edit postmortems for incidents.",
        "Postmortems",
    ),
    PermissionSpec(
        "postmortem:approve",
        "Approve postmortems",
        "Review and approve postmortems so they become part of the knowledge base.",
        "Postmortems",
    ),
    PermissionSpec(
        "knowledge:review",
        "Review knowledge",
        "Approve or reject knowledge proposed by EKIP before it is published.",
        "Knowledge",
    ),
    PermissionSpec(
        "observability:read",
        "View observability",
        "See agent activity, usage and platform health views.",
        "Observability",
    ),
    PermissionSpec(
        "audit:read",
        "View audit log",
        "Read the organization's audit trail of who changed what.",
        "Administration",
    ),
    PermissionSpec(
        "tenancy:manage",
        "Manage organization",
        "Full administration: users, roles, invitations, access rules, connectors, "
        "SSO and projects. Holders of this permission are the organization's administrators.",
        "Administration",
    ),
)

ASSIGNABLE_PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in ASSIGNABLE_PERMISSIONS)

#: The permission that makes someone an organization administrator (see
#: `core.tenancy.service`'s module docstring: admin-ness is holding this code).
ADMIN_PERMISSION: str = "tenancy:manage"

#: The immutable, platform-wide role every organization shares.
BUILTIN_ADMIN_ROLE_NAME: str = "admin"
BUILTIN_ADMIN_ROLE_DESCRIPTION: str = "Full-access role granted to an organization's first user."


@dataclass(frozen=True)
class RoleTemplate:
    name: str
    description: str
    permissions: tuple[str, ...]


#: The starter roles created (editable, deletable) for every organization.
DEFAULT_ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        "viewer",
        "Read-only access to incidents and observability.",
        ("incident:read", "observability:read"),
    ),
    RoleTemplate(
        "engineer",
        "Works incidents: report and update them and write postmortems.",
        ("incident:read", "incident:write", "postmortem:write", "observability:read"),
    ),
    RoleTemplate(
        "reviewer",
        "Reviews and approves postmortems and proposed knowledge.",
        ("incident:read", "observability:read", "postmortem:approve", "knowledge:review"),
    ),
)


def permission_spec(code: str) -> PermissionSpec | None:
    for spec in ASSIGNABLE_PERMISSIONS:
        if spec.code == code:
            return spec
    return None
