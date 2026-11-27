"""This module defines the Pydantic schemas for `core/users`. `Permission` and `Role` are 
created directly from their database ORM objects, while `UserProfile` is built by 
combining a user record with its roles and permissions. The key difference is that 
`Identity` is a small, security-focused object used to check permissions during every 
request, whereas `UserProfile` contains richer user information for display and 
user-management endpoints. They serve different purposes: authorization versus display 
and administration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    #: This organization's access state for the member: `active` or `deactivated` (never the global flag).
    membership_status: str = "active"
    #: Who deactivated them: `admin` or `scim`. None while active.
    membership_source: str | None = None
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime


class OrganizationMember(BaseModel):
    """One user's membership summary within a single organization -- the
    read surface backing `GET /organizations/{id}/members` Deliberately not `UserProfile`: this is a listing
    of other users seen by an admin, not "my own resolved access," so it
    carries `roles` only (never a flattened `permissions` set for someone
    else's account) and omits anything password/credential-adjacent.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    #: This organization's access state for the member: `active` or `deactivated` (never the global flag).
    membership_status: str = "active"
    #: Who deactivated them: `admin` or `scim`. None while active.
    membership_source: str | None = None
    roles: tuple[str, ...] = ()
    #: Ids of the roles the member holds -- what the role editor needs to
    #: preselect their current roles (order is not significant).
    role_ids: tuple[uuid.UUID, ...] = ()
    created_at: datetime


class UserCredentialLookup(BaseModel):
    """The minimum a caller verifying a password credential needs -- not a
    full `User` row.

    Exists so `core.auth.service` (which owns hashing/verification "core/auth verifies authentication") can check a
    password without core/users handing another module a raw ORM row, or
    core/auth reaching into `core.users.repository` directly (breaking the
    "call the owning layer's service.py, never its repository.py" convention
    every other cross-module call in this codebase follows).
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    email_verified_at: datetime | None = None
    password_hash: str | None
    is_active: bool
    email: str | None = None

class UserEmailState(BaseModel):
    """Narrow view of a user's email-ownership state for the verification
    service -- never a raw ORM row, never the password hash."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    email: str
    email_verified_at: datetime | None
    is_active: bool
    has_password: bool


# --- organization membership state (member offboarding) -------------------------------------------------


class MemberStatus(BaseModel):
    """One member's access state in ONE organization. `status` is `active` when no state row exists.
    `reason` and `source` are shown only to callers holding `tenancy:manage` (the API requires it)."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    organization_id: uuid.UUID
    status: str
    source: str | None = None
    reason: str | None = None
    deactivated_at: datetime | None = None
    updated_at: datetime | None = None
    member_epoch: int = 0


class MemberStatusChange(BaseModel):
    """Request body for deactivating or reactivating a member. A reason is mandatory."""

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A reason is required.")
        return stripped
