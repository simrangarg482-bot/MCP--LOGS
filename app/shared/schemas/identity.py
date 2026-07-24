"""The `Identity` value object threaded through every cross-module call.

Owned by: shared/ (ARCHITECTURE.md section 3). This is the single most
load-bearing shared type in the system: API_DESIGN.md section 2 threads
`Identity` through every `core/` and `agents/` public call precisely so that
access control is identical regardless of entry point -- the same
`authorize()` check runs whether a request arrived over REST or MCP
(ARCHITECTURE.md section 6).

It is deliberately transport-agnostic: it carries *who* the caller is and
*what* they're allowed to do, with no knowledge of JWTs, MCP tokens, or HTTP.
Resolving a raw credential into an `Identity` is core/auth/'s job; everything
downstream just receives the resolved object.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ActorKind(str, Enum):
    """Distinguishes human users from non-human callers.

    Mirrors the human-vs-AI authorship distinction that DATABASE_DESIGN.md
    keeps unambiguous at the data layer (the tagged `actor` strings on
    incident_timeline / postmortems / audit_logs). `Identity.audit_tag`
    produces exactly those strings, so the identity that performed an action
    and the string recorded against it never drift apart.
    """

    USER = "user"
    SERVICE = "service"
    AGENT = "agent"


class Identity(BaseModel):
    """A resolved caller. Immutable once constructed.

    `permissions` is the flattened set of permission codes granted via the
    caller's roles (resolved once by core/users/), so downstream `authorize()`
    checks are a pure set-membership test with no further database access.
    """

    # Frozen: an identity is a fact about who is calling, resolved once at the
    # boundary; nothing downstream should be able to mutate it (e.g. quietly
    # grant itself a permission mid-request).
    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    # The stable principal id: a stringified user UUID, a service name, or an
    # agent name -- whatever uniquely identifies this caller of `kind`.
    subject: str
    # Populated only when kind == USER; None for services/agents that have no
    # `users` row (consistent with DATABASE_DESIGN.md's rationale for tagged
    # actors rather than strict FKs).
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = Field(default_factory=frozenset)

    @property
    def audit_tag(self) -> str:
        """The `actor` string recorded in incident_timeline / audit_logs / etc.

        e.g. ``user:3f2a...`` or ``agent:postmortem_agent``. Single source of
        that format, so it stays consistent everywhere it is written.
        """
        return f"{self.kind.value}:{self.subject}"

    def has_permission(self, permission_code: str) -> bool:
        """Pure, database-free permission check against the pre-resolved set."""
        return permission_code in self.permissions

    @classmethod
    def for_agent(cls, agent_name: str) -> "Identity":
        """Convenience constructor for internal agent-initiated calls.

        Agents act with no interactive user (AGENT_WORKFLOWS.md); this keeps
        their identity construction in one place rather than scattered literal
        ``Identity(kind=ActorKind.AGENT, ...)`` calls across agents/.
        """
        return cls(kind=ActorKind.AGENT, subject=agent_name)