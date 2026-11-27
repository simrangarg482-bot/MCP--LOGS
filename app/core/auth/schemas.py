"""Pydantic contracts for core/auth.

Owned by: core/auth. Local to this submodule (PROJECT_STRUCTURE.md), same
pattern as core/tenancy/schemas.py and core/users/schemas.py.

Supersedes API_DESIGN.md's original `POST /auth/login` / `AskRequest`-style
single-tenant shapes: per PROJECT_PLAN.md's opening note, anything touching
authentication is governed by this document's section 3.3-3.4, not the older
one. The flow modeled here is OIDC Authorization Code + PKCE, provider-
agnostic across Entra ID / Okta / Auth0 / Google Workspace (section 3.3) --
one shape handles all four, since all four speak OIDC.

Two round trips, two schema pairs:
  1. Begin login (`SSOAuthorizationRedirect`) -> employee's browser goes to
     the IdP -> IdP redirects back with a code (`SSOCallbackRequest`).
  2. `complete_sso_login` returns `SessionTokens`; `refresh` takes a
     `RefreshRequest` and also returns `SessionTokens`; `verify_access_token`
     returns `TokenClaims`.

PKCE mechanics: the `code_verifier` (and the OIDC `nonce`) are generated when
login begins and kept server-side in a short-lived, single-use Redis record
(`core.auth.sso_transaction`); the browser only ever holds an opaque
`transaction_id`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class SSOAuthorizationRedirect(BaseModel):
    """Result of beginning an SSO login for one organization.

    `authorization_url` is where the browser should be sent -- the identity
    provider's authorize endpoint, pre-filled with the organization's client id,
    the PKCE challenge, the `state` and the OIDC `nonce`. `state` is echoed back
    by the provider so the page can sanity-check the round trip, but the
    authoritative check is server-side. `transaction_id` is an opaque handle to
    a short-lived, single-use server record (see `core.auth.sso_transaction`)
    that holds the PKCE verifier and nonce -- neither is ever sent to the
    browser.
    """

    model_config = ConfigDict(frozen=True)

    authorization_url: str
    state: str
    transaction_id: str


class SSOCallbackRequest(BaseModel):
    """Input to `complete_sso_login`, once the IdP has redirected back.

    `org_slug` must match the organization the transaction was started for;
    `state` must equal the transaction's state; `transaction_id` is the handle
    `SSOAuthorizationRedirect` returned. All of it is verified server-side.
    """

    org_slug: str = Field(min_length=1, max_length=63)
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=512)
    transaction_id: str = Field(min_length=1, max_length=128)


class SSOOptions(BaseModel):
    """What the public login page may learn about an organization's SSO.

    Deliberately minimal and identical in shape for "no such organization", "no
    SSO", "SSO disabled" and "organization suspended": `available=false` with
    no provider. Never the issuer, client id, secret or organization name.
    """

    model_config = ConfigDict(frozen=True)

    available: bool
    provider: str | None = None
    provider_label: str | None = None


class SessionTokens(BaseModel):
    """EKIP's own signed session, issued after a successful login or refresh
    (PROJECT_PLAN.md section 3.4).

    `access_token` is the short-lived JWT verified on every request (by
    `verify_access_token`, and used identically by the REST API and MCP per
    section 7.4); `refresh_token` is the longer-lived credential used to
    obtain a new `access_token` without repeating the full SSO round trip.

    `status` is additive, for the multi-organization login design gap fix:
    `POST /auth/login` used to always return exactly this shape, so a
    literal, defaulted `"complete"` here keeps every existing caller working
    unchanged (they simply never look at a field they didn't know existed)
    while letting the endpoint's response type become
    `SessionTokens | OrganizationSelectionRequired` -- a multi-organization
    user gets the latter instead of silently receiving a token for whichever
    organization happened to come back first. `Field(discriminator="status")`
    on that union is what lets FastAPI/Pydantic tell the two apart cleanly.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["complete"] = "complete"
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until access_token expires, from issuance


class RefreshRequest(BaseModel):
    """Input to `refresh` -- exchange a still-valid refresh token for a new
    `SessionTokens` pair.
    """

    refresh_token: str


class SignupRequest(BaseModel):
    """Input to `signup` -- self-service email/password account creation.

    Always creates a brand-new organization alongside the user (there is no
    "join an existing organization via signup" flow yet -- see `signup`'s
    own docstring); `organization_slug` follows `OrganizationCreate.slug`'s
    exact URL-safe pattern, since it becomes that organization's real slug.
    """

    email: str
    # Policy (12-128 characters) is enforced by `core.auth.passwords` so the error carries a
    # stable `error_code`; this bound only stops absurd payloads.
    password: str = Field(min_length=1, max_length=1024)
    display_name: str
    organization_name: str
    organization_slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", min_length=1, max_length=63)


class LoginRequest(BaseModel):
    """Input to `login_with_password`. No length POLICY here (an existing 8-character
    password must still work); the bound only stops absurd payloads."""

    email: str
    password: str = Field(min_length=1, max_length=1024)


class OrganizationSummary(BaseModel):
    """The minimal, non-sensitive shape of an organization a person can pick
    from -- used both by the multi-organization login flow and by
    `GET /auth/organizations`. Deliberately smaller than
    `core.tenancy.schemas.OrganizationRead`: this is what a user chooses
    between at login time, not an admin-facing organization record.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    slug: str
    #: Read-only display of the organization's lifecycle state (`onboarding` / `active`; a suspended
    #: organization is never offered). Only a platform operator can change it.
    status: str | None = None


class OrganizationSelectionRequired(BaseModel):
    """Returned by `login_with_password` instead of `SessionTokens` when the
    authenticated user belongs to more than one organization -- the whole
    point of this design-gap fix: password authentication having succeeded
    no longer implies an organization has been chosen, so no access token is
    issued yet.

    `selection_token` is a short-lived, single-purpose token (see
    `_issue_org_selection_token`/`verify_org_selection_token`) that proves
    the password check already succeeded for a specific `user_id`, without
    itself granting access to anything -- `POST /auth/select-organization`
    exchanges it plus a chosen `organization_id` for a real `SessionTokens`,
    but only after re-verifying that `user_id` actually belongs to that
    organization. It is NOT an access token and `verify_access_token` will
    not accept it (see that function's `"type"` claim check).
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["organization_selection_required"] = "organization_selection_required"
    selection_token: str
    organizations: tuple[OrganizationSummary, ...]


class OrganizationSelectionRequest(BaseModel):
    """Input to `POST /auth/select-organization` -- the second step of a
    multi-organization login, exchanging the `selection_token` an
    `OrganizationSelectionRequired` response carried plus the user's chosen
    `organization_id` for a real `SessionTokens`. The backend re-verifies
    user-to-organization membership itself; `organization_id` here is a
    request, never a trusted assertion.
    """

    selection_token: str
    organization_id: uuid.UUID


class SwitchOrganizationRequest(BaseModel):
    """Input to `POST /auth/switch-organization` -- for an already
    authenticated user (a valid access token, any organization) asking to
    be re-scoped to a different organization they also belong to.

    Per the security requirement this flow exists to satisfy: the backend
    authenticates the caller from their *existing* access token, then
    independently verifies caller-to-`organization_id` membership before
    issuing anything -- this request's `organization_id` is never written
    directly into a token.
    """

    organization_id: uuid.UUID


class OrganizationsListResponse(BaseModel):
    """Response for `GET /auth/organizations` -- every organization the
    *currently authenticated* caller belongs to (derived from their verified
    identity, never from a client-supplied user id), for building an
    organization switcher in the frontend.
    """

    model_config = ConfigDict(frozen=True)

    organizations: tuple[OrganizationSummary, ...]


LoginResponse = Annotated[
    Union[SessionTokens, OrganizationSelectionRequired], Field(discriminator="status")
]
"""`POST /auth/login`'s actual response type: `SessionTokens` for a
single-organization user (unchanged, existing behavior) or
`OrganizationSelectionRequired` for a multi-organization one. The
`discriminator="status"` tells FastAPI/Pydantic which of the two a given
response body is without guessing from shape.
"""


class LogoutAllResponse(BaseModel):
    """Response for `POST /auth/logout-all` and `POST /users/{user_id}/
    logout-all` -- "logout everywhere" (`revoke_all_sessions`).

    `revoked_session_count` is `revoke_all_sessions`'s own return value (the
    number of `refresh_tokens` rows it revoked), included alongside the
    human-readable `message` so a caller can tell "nothing to revoke" (0)
    apart from "some sessions really were revoked" without parsing prose.

    Important, and stated here rather than only in the endpoint docstring:
    this revokes refresh tokens, not already-issued access tokens.
    `core.auth.service.verify_access_token` is stateless (pure JWT signature/
    expiry check, no database lookup) -- an access token issued before this
    call remains valid until its own `exp` (bounded by `settings.
    jwt_expiry_minutes`), even after every refresh token is revoked. "Logged
    out everywhere" therefore means "no session can be *refreshed* past this
    point," not "every existing access token stops working immediately."
    """

    model_config = ConfigDict(frozen=True)

    message: str
    revoked_session_count: int


class TokenClaims(BaseModel):
    """The verified, decoded claims of an access token -- the output of
    `verify_access_token`.

    This is intentionally smaller than `shared.schemas.Identity`: it is the
    raw, transport-level claim set (who, which organization, when it expires)
    with no roles/permissions resolved yet. Turning this into a full
    `Identity` is `core.users.service.resolve_identity`'s job -- core/auth
    answers "whose token is this and is it valid," not "what can they do"
    (mirrors the existing division of labor already documented on
    core/users/service.resolve_identity).
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    organization_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    #: OAuth scopes carried by the token (`scope` claim, space-separated in the
    #: JWT). Empty for an ordinary REST session token.
    scopes: tuple[str, ...] = ()
    #: The organization session epoch (`oe` claim) the token was issued under. A token minted before the
    #: claim existed has none and counts as epoch 0.
    org_session_epoch: int = 0
    #: The member session epoch (`me` claim): moves on every deactivation and reactivation of this person's
    #: membership in this organization. A token minted before the claim existed has none and counts as 0.
    member_epoch: int = 0


class VerifiedIdPClaims(BaseModel):
    """The verified claims extracted from an IdP's ID token, once
    `_exchange_code_for_claims` has completed signature and configured
    issuer/audience/expiry verification.

    Distinct from `TokenClaims` (EKIP's own access token claims): this is
    what the *IdP* asserts about the user -- subject, email, display name,
    and group memberships if the provider sends them -- before EKIP has
    decided anything about provisioning. This is the boundary of core/auth's
    responsibility in the SSO-provisioning-policy design
    (ENGINEERING_DECISIONS.md): "verify OIDC authentication, validate token
    claims, extract identity information." Deciding whether these claims are
    *allowed* to provision an account in a given organization is
    core/tenancy's job (`evaluate_provisioning`), not this schema's or
    core/auth's.

    `groups` defaults to empty, not `None`: an IdP that doesn't send a groups
    claim at all is indistinguishable, from core/auth's point of view, from
    one that sent an empty group list -- both simply mean "no group-based
    provisioning signal available for this login."
    """

    model_config = ConfigDict(frozen=True)

    sub: str
    email: str
    name: str | None = None
    groups: tuple[str, ...] = ()
    #: True ONLY if the IdP affirmatively proved this email's ownership under
    #: that provider's rules (`core.auth.service._idp_email_verification`).
    #: Defaults to False (fail closed): a claims object built without the
    #: evidence can never provision, match an invitation, or link an account.
    email_verified: bool = False
    #: Which claim supplied the evidence (`email_verified` / `xms_edov`), for
    #: audit logs only.
    email_verification_source: str | None = None


class VerificationRequested(BaseModel):
    """Generic acknowledgement for signup, invitation acceptance and resend.

    Deliberately identical whether or not a message was actually sent (an
    address that already has an account, or a rate-limited resend, produce
    the same body) so these endpoints cannot be used to enumerate accounts.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["verification_sent"] = "verification_sent"
    message: str = (
        "If this address can be used, a confirmation email is on its way. "
        "Open the link in it to continue."
    )


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class VerifyEmailResponse(BaseModel):
    """Result of consuming a verification token. Carries NO session: the
    user signs in through the normal login endpoint afterwards."""

    model_config = ConfigDict(frozen=True)

    status: Literal["verified"] = "verified"


class ResendVerificationRequest(BaseModel):
    email: str = Field(max_length=320)


class ForgotPasswordRequest(BaseModel):
    """Input to `POST /auth/password/forgot`."""

    email: str = Field(max_length=320)


class PasswordResetRequested(BaseModel):
    """Generic acknowledgement for "forgot password". Identical whether or not an
    account exists, has a password, or a mail was actually sent."""

    model_config = ConfigDict(frozen=True)

    status: Literal["reset_requested"] = "reset_requested"
    message: str = "If that address belongs to an account that can sign in with a password, a reset link is on its way."


class ResetPasswordRequest(BaseModel):
    """Input to `POST /auth/password/reset`: the emailed token and the new password."""

    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=1024)


class PasswordResetCompleted(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["password_reset"] = "password_reset"
    message: str = "Your password was changed. Sign in with the new password."


class ChangePasswordRequest(BaseModel):
    """Input to `POST /auth/password/change` (authenticated)."""

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)
