"""Pydantic contracts for core/users.

Local to the users submodule (PROJECT_STRUCTURE.md). `Permission` and `Role`
map directly from their ORM rows; `UserProfile` is an aggregate the service
composes from a user row plus its role/permission joins, so it is built
explicitly rather than validated from a single ORM object.

Note the relationship to `shared.schemas.Identity`: `Identity` is the compact,
security-focused object threaded through *every* call (kind + permission set);
`UserProfile` is the richer, human-facing representation returned by user-
management / "who am I" endpoints. They are intentionally distinct -- one is
for authorization checks, the other for display and administration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Permission(BaseModel):
    """A single permission code, e.g. `incident:write`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    code: str
    description: str | None = None


class Role(BaseModel):
    """A named role, e.g. `incident_commander`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    name: str
    description: str | None = None


class UserProfile(BaseModel):
    """A user together with their resolved access.

    `roles` is the list of assigned role names; `permissions` is the flattened,
    de-duplicated, sorted set of permission codes those roles grant -- the same
    set that lands in `Identity.permissions`. Sorted so the representation is
    deterministic (stable API responses, stable test assertions).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime