"""Domain error types raised by the core/ service layer.

Owned by: core/. These are transport-agnostic: a service raises `NotFoundError` or `PermissionDeniedError` without knowing whether the caller arrived over REST or MCP. The api/ and mcp/ boundary layers each catch `EKIPError` and translate `.error_code` / `.status_hint` into their own protocol -- an HTTP status code, or an MCP error response -- so REST and MCP share one set of error semantics (ARCHITECTURE.md section 6).

Why a small explicit hierarchy rather than raising built-ins (ValueError, KeyError): the boundary layer needs a stable, machine-readable `error_code` and a status mapping that does not depend on guessing from a built-in type. Keeping the mapping on the exception (via `status_hint`) means the transport layer stays a thin translation with no per-error `if isinstance(...)` ladder.
"""

from __future__ import annotations

from app.shared.schemas import ErrorBody


class EKIPError(Exception):
    """Base for every deliberate, expected domain error in core/.

    Unexpected failures (bugs, dropped DB connections) should NOT be raised as
    `EKIPError` -- they propagate as ordinary exceptions and become a 500 at
    the boundary. `EKIPError` is exclusively for conditions the domain models
    on purpose (missing resource, denied permission, conflict, bad input).
    """

    #: Conventional HTTP status the boundary maps this to (API_DESIGN.md
    #: "Design conventions"). MCP uses the same value to pick its error shape.
    status_hint: int = 500
    #: Stable, machine-readable code, e.g. "incident.not_found". Subclasses set
    #: a default prefix; callers may override per raise for specificity.
    default_error_code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.default_error_code
        self.detail = detail

    def to_error_body(self) -> ErrorBody:
        """Render into the single wire error shape from API_DESIGN.md.

        The boundary layer serializes this directly; nothing here knows about
        HTTP responses or MCP envelopes.
        """
        return ErrorBody(
            error_code=self.error_code,
            message=self.message,
            detail=self.detail,
        )


class ValidationError(EKIPError):
    """Input failed a domain rule that Pydantic alone could not enforce.

    (Pure shape/type validation belongs in the Pydantic schema; this is for
    cross-field or state-dependent rules, e.g. an illegal status transition.)
    """

    status_hint = 400
    default_error_code = "validation_error"


class PermissionDeniedError(EKIPError):
    """The caller's `Identity` lacks the required permission code.

    Raised by `core.authorize`'s enforcing wrapper, so every entry point
    (REST, MCP) denies access identically (ARCHITECTURE.md section 6).
    """

    status_hint = 403
    default_error_code = "permission_denied"


class NotFoundError(EKIPError):
    """A referenced resource does not exist (or is soft-deleted / hidden)."""

    status_hint = 404
    default_error_code = "not_found"


class ConflictError(EKIPError):
    """The request conflicts with current state.

    e.g. a uniqueness violation, or an operation invalid for the resource's
    current lifecycle state (approving an already-published postmortem).
    """

    status_hint = 409
    default_error_code = "conflict"