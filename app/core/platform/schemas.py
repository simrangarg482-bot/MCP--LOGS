"""Schemas for platform-operator authentication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OperatorRole = Literal["admin", "viewer"]


class PlatformLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class PlatformSession(BaseModel):
    """A platform login result. There is deliberately NO refresh token: when the (short-lived) access
    token expires the operator signs in again."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: OperatorRole


class PlatformOperator(BaseModel):
    """An authenticated, currently-active platform operator (re-verified on every request)."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    role: OperatorRole

    @property
    def can_transition(self) -> bool:
        return self.role == "admin"


class PlatformTokenClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime


# --- organization lifecycle (Phase 5) ---------------------------------------------------------------------

OrganizationStatus = Literal["onboarding", "active", "suspended"]


class PlatformOrganizationSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: OrganizationStatus
    status_changed_at: datetime | None
    created_at: datetime
    member_count: int


class PlatformOrganizationList(BaseModel):
    items: list[PlatformOrganizationSummary]
    total: int
    limit: int
    offset: int


class PlatformOrganizationDetail(PlatformOrganizationSummary):
    #: The customer-facing text shown to the organization's members while suspended.
    status_message_public: str | None
    session_epoch: int
    history_count: int


class PlatformStatusHistoryEntry(BaseModel):
    id: uuid.UUID
    previous_status: OrganizationStatus
    new_status: Literal["active", "suspended"]
    reason: str
    customer_message: str | None
    #: Private to operators. Never exposed by any tenant API.
    internal_note: str | None
    operator_user_id: uuid.UUID
    operator_email: str | None
    changed_at: datetime
    request_id: str | None


class PlatformStatusHistory(BaseModel):
    items: list[PlatformStatusHistoryEntry]
    total: int
    limit: int
    offset: int


class ActivateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReactivateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    #: Shown to the organization's members and administrators.
    customer_message: str | None = Field(default=None, max_length=1000)
    #: Operators only.
    internal_note: str | None = Field(default=None, max_length=4000)


class StatusChangeResult(BaseModel):
    organization: PlatformOrganizationDetail
    previous_status: OrganizationStatus
    new_status: Literal["active", "suspended"]
    history_id: uuid.UUID
    #: Whether administrators will be emailed (delivery itself happens after the response and cannot
    #: undo this change).
    notification_queued: bool
