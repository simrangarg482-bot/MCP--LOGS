"""The SCIM error type. Rendered by `app.api.main` as an RFC 7644 error body with `application/scim+json`."""

from __future__ import annotations


class ScimError(Exception):
    """An error answered to the identity provider.

    `status` is the HTTP status, `detail` a human sentence (never contains a token, secret or another
    tenant's data), `scim_type` one of RFC 7644's `scimType` values when one applies, and `code` the short
    machine code recorded as the organization's `last_error_code` and in audit events.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        scim_type: str | None = None,
        *,
        code: str = "scim.error",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.code = code
        self.headers = headers or {}

    def body(self) -> dict:
        body: dict = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": str(self.status),
            "detail": self.detail,
        }
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body
