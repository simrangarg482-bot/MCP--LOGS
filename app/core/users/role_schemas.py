"""Pydantic models for role management (the `core.users.role_service` surface)."""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Letters/digits first, then letters, digits, space, `_`, `.`, `-`. Keeps names
#: printable and unambiguous in the UI, the audit log and the invitation form.
_ROLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]*$")
ROLE_NAME_MIN = 2
ROLE_NAME_MAX = 64
ROLE_DESCRIPTION_MAX = 300


def _normalize_name(value: str) -> str:
    name = " ".join(value.split())  # trim + collapse internal whitespace
    if not (ROLE_NAME_MIN <= len(name) <= ROLE_NAME_MAX):
        raise ValueError(f"Role name must be {ROLE_NAME_MIN}-{ROLE_NAME_MAX} characters.")
    if not _ROLE_NAME_PATTERN.match(name):
        raise ValueError(
            "Role name may contain letters, numbers, spaces, '_', '.' and '-', "
            "and must start with a letter or number."
        )
    return name


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) > ROLE_DESCRIPTION_MAX:
        raise ValueError(f"Description must be at most {ROLE_DESCRIPTION_MAX} characters.")
    return text or None


class PermissionInfo(BaseModel):
    """One assignable permission, as shown on the roles page."""

    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    description: str
    group: str


class Role(BaseModel):
    """A role visible to an organization, with its permissions and usage."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    #: True for platform-wide roles (`admin`): read-only for everyone.
    is_builtin: bool
    permissions: tuple[str, ...] = ()
    #: Number of members of the requesting organization holding this role.
    member_count: int = 0


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _normalize_name(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return _normalize_description(value)


class RoleUpdate(BaseModel):
    """Partial update: only fields that are present are changed."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_name(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return _normalize_description(value)


class MemberRoleAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: uuid.UUID


class MemberRolesSet(BaseModel):
    """Replace a member's complete role set. Must contain at least one role."""

    model_config = ConfigDict(extra="forbid")

    role_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class MemberRoles(BaseModel):
    """A member's roles after a change."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    role_ids: tuple[uuid.UUID, ...]
    roles: tuple[str, ...]
