"""`core/auth` handles SSO login, verifies users' identity tokens, creates sessions,
and manages token refresh. It works with `core/tenancy` to check the organization's SSO
and provisioning rules, and `core/users` to find or create the user and assign a role.
The OIDC login and secret decryption logic are implemented, but login has not yet been
tested end-to-end with a real identity provider. Group-based provisioning reads only the
standard `groups` claim, which suitably configured Entra ID or Okta deployments may
provide. Auth0 custom/namespaced group claims are not extracted, and Google Workspace
group membership requires a separate Admin SDK integration that is not implemented here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import NoReturn
from urllib.parse import urlencode, urlparse

import bcrypt
import httpx
from jose import JWTError, jwt as jose_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import email_verification, passwords, repository, security_events, sso_transaction
from app.core.auth.schemas import (
    LoginRequest,
    LoginResponse,
    OrganizationSelectionRequest,
    OrganizationSelectionRequired,
    OrganizationsListResponse,
    OrganizationSummary,
    ChangePasswordRequest,
    RefreshRequest,
    SessionTokens,
    SignupRequest,
    SSOAuthorizationRedirect,
    SSOCallbackRequest,
    SSOOptions,
    SwitchOrganizationRequest,
    TokenClaims,
    VerifiedIdPClaims,
)
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationAcceptRequest, OrganizationCreate, SSOConfiguration
from app.core.scim import service as scim_service
from app.core.users import membership
from app.core.users import service as users_service
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.organization_status import is_suspended
from app.shared.outbound_url import (
    UnsafeOutboundUrlError,
    assert_public_https_url,
    issuers_equal,
    normalize_issuer,
)
from app.shared.security import decrypt_secret, get_kms, hash_opaque_token

def _hash_password(password: str) -> str:
    """Hash a NEW password for `users.password_hash` -- Argon2id (see
    `core.auth.passwords`, which also owns the length policy). Synchronous and
    memory-hard: async callers use `asyncio.to_thread`."""
    return passwords.hash_password(password)


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a stored hash: Argon2id, or a legacy bcrypt hash
    made before the Argon2id migration (`passwords.needs_rehash` then upgrades it
    after a successful login). Constant-time in the underlying libraries."""
    return passwords.verify_password(password, password_hash)


logger = get_logger(__name__)

# Refresh-token lifetime is not yet a Settings field (only jwt_expiry_minutes,
# for the access token, exists there today) -- a reasonable default is used
# here, called out as a natural small follow-up addition to
# shared/config/settings.py rather than made here, to keep this migration to
# one file's concern.
_REFRESH_TOKEN_LIFETIME = timedelta(days=30)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# In-process cache of each issuer's OIDC discovery document, keyed by
# issuer_url. A discovery document changes rarely, so refetching it on every
# single login would add an avoidable round trip; the TTL just bounds how
# long a provider-side change (e.g. a key rotation reflected in a new
# jwks_uri) takes to be picked up. NOTE: per-process only -- if core/auth
# ever runs across multiple worker processes, each keeps its own copy. A
# shared cache (Redis, already a dependency per ENGINEERING_DECISIONS.md
# #002) would avoid duplicate fetches across processes; not wired up here to
# keep this change scoped to the OIDC adapter itself, not a caching redesign.
_DISCOVERY_CACHE_TTL = timedelta(hours=1)
_discovery_cache: dict[str, tuple[dict, datetime]] = {}


# --- SSO login: begin -----------------------------------------------------------

# Signing algorithms an ID token may use. Asymmetric only: `none` and the HMAC
# family (where the "key" would be public / shared) are never accepted, whatever
# the token header or the provider's JWKS claims.
_ALLOWED_ID_TOKEN_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")
_KEY_TYPE_FOR_ALGORITHM = {
    "RS256": "RSA",
    "RS384": "RSA",
    "RS512": "RSA",
    "ES256": "EC",
    "ES384": "EC",
    "ES512": "EC",
}

SSO_PROVIDER_LABELS = {
    "entra_id": "Microsoft Entra ID",
    "okta": "Okta",
    "auth0": "Auth0",
    "google_workspace": "Google Workspace",
}


def _allow_insecure_issuers() -> bool:
    return get_settings().sso_allow_insecure_issuers


def _assert_redirect_uri_allowed(redirect_uri: str) -> None:
    """Reject a `redirect_uri` whose origin isn't in `cors_allowed_origins`
    -- the same trusted-frontend-origin allowlist already governing which
    origins may call this API from a browser (`Settings.cors_allowed_origins`).

    This is only the origin half; `_assert_callback_uri_allowed` additionally
    pins the exact callback path, and is what both SSO endpoints call.
    """
    settings = get_settings()
    parsed = urlparse(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in settings.cors_allowed_origins:
        raise PermissionDeniedError(
            "redirect_uri is not an allowed origin for this deployment.",
            error_code="auth.redirect_uri_not_allowed",
            detail={"redirect_uri": redirect_uri},
        )


def _assert_callback_uri_allowed(redirect_uri: str, org_slug: str) -> None:
    """The redirect URI must be exactly `<allowed origin>/o/<org_slug>/callback`
    -- no other path, no query string, no fragment. Combined with the identity
    provider's own registered-redirect check, a login can only ever return to
    the one page built to complete it."""
    _assert_redirect_uri_allowed(redirect_uri)
    parsed = urlparse(redirect_uri)
    if (
        parsed.path != f"/o/{org_slug}/callback"
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise PermissionDeniedError(
            "redirect_uri is not the callback address for this organization.",
            error_code="auth.redirect_uri_not_allowed",
        )


def _idp_unreachable(reason: str) -> ServiceUnavailableError:
    # Generic on purpose: callers are anonymous and the cause (an unsafe URL, a
    # timeout, a bad document) is for our logs, not for them.
    logger.warning("sso_idp_request_failed", reason=reason)
    return ServiceUnavailableError(
        "The identity provider could not be reached or returned an unusable response. "
        "Try again shortly; if it persists, contact your administrator.",
        error_code="auth.idp_unreachable",
    )


async def _assert_outbound_url_safe(url: str) -> None:
    """SSRF guard for every URL the identity provider (or an admin) supplies.
    A seam so tests that use fake, non-resolving hosts can replace it."""
    try:
        await assert_public_https_url(url, allow_insecure=_allow_insecure_issuers())
    except UnsafeOutboundUrlError as exc:
        raise _idp_unreachable(f"unsafe url: {exc}") from exc


def _sso_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=get_settings().sso_http_timeout_seconds, follow_redirects=False
    )


async def get_sso_options(session: AsyncSession, org_slug: str) -> SSOOptions:
    """What the public login page may know: is SSO usable for `org_slug`, and with
    which provider. Every "no" -- unknown organization, suspended, not
    configured, disabled, unsupported protocol -- yields the same answer."""
    try:
        config = await tenancy_service.get_organization_sso_config(session, org_slug)
    except NotFoundError:
        return SSOOptions(available=False)
    return SSOOptions(
        available=True,
        provider=config.provider,
        provider_label=SSO_PROVIDER_LABELS.get(config.provider, config.provider),
    )


async def begin_sso_login(
    session: AsyncSession,
    org_slug: str,
    *,
    redirect_uri: str,
    store: sso_transaction.SsoTransactionStore | None = None,
) -> SSOAuthorizationRedirect:
    """Start an SSO login for the organization identified by `org_slug`.

    Creates a single-use server-side transaction (state, nonce, PKCE verifier,
    organization, redirect URI) and returns the provider URL plus an opaque
    handle. `redirect_uri` must be exactly this organization's callback address
    on an allowed origin. Raises `sso.unavailable` (identical for every cause)
    when SSO cannot be used for this organization.
    """
    _assert_callback_uri_allowed(redirect_uri, org_slug)
    sso_config = await tenancy_service.get_organization_sso_config(session, org_slug)
    authorization_endpoint = await _discover_authorization_endpoint(sso_config.issuer_url)

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    try:
        transaction_id = await sso_transaction.create_transaction(
            store or sso_transaction.get_transaction_store(),
            org_slug=org_slug,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            ttl_seconds=get_settings().sso_transaction_ttl_seconds,
        )
    except sso_transaction.SsoTransactionUnavailableError as exc:
        logger.error("sso_transaction_store_unavailable")
        raise ServiceUnavailableError(
            "Single sign-on is temporarily unavailable. Try again shortly.",
            error_code="auth.sso_unavailable",
        ) from exc

    authorization_url = _build_authorization_url(
        authorization_endpoint,
        client_id=sso_config.client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        nonce=nonce,
    )
    return SSOAuthorizationRedirect(
        authorization_url=authorization_url, state=state, transaction_id=transaction_id
    )


def _pkce_challenge(code_verifier: str) -> str:
    """Derive the S256 PKCE code_challenge from a code_verifier (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    """Build the IdP authorization URL. Pure string construction -- no
    network call, unlike the two OIDC seams below.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


async def _get_discovery_document(issuer_url: str) -> dict:
    """Fetch (and cache) an issuer's validated OIDC discovery document.

    All four supported providers publish one at
    `{issuer_url}/.well-known/openid-configuration`, which is exactly why one
    code path can handle all four -- this function doesn't special-case any
    provider.

    The issuer is normalized (trailing slash, case of scheme/host), the request
    is refused for non-public destinations (`_assert_outbound_url_safe`),
    redirects are not followed, and the document's own `issuer` must equal the
    issuer it was fetched for (OIDC Discovery 4.3) -- otherwise a provider could
    vouch for someone else's tokens. Failures become `auth.idp_unreachable`; a
    failed fetch is never cached.
    """
    try:
        issuer = normalize_issuer(issuer_url, allow_insecure=_allow_insecure_issuers())
    except UnsafeOutboundUrlError as exc:
        raise _idp_unreachable(f"invalid issuer: {exc}") from exc

    cached = _discovery_cache.get(issuer)
    if cached is not None:
        document, cached_at = cached
        if datetime.now(timezone.utc) - cached_at < _DISCOVERY_CACHE_TTL:
            return document

    discovery_url = f"{issuer}/.well-known/openid-configuration"
    await _assert_outbound_url_safe(discovery_url)
    try:
        async with _sso_http_client() as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _idp_unreachable(f"discovery fetch failed: {type(exc).__name__}") from exc

    if not isinstance(document, dict) or not issuers_equal(
        document.get("issuer"), issuer, allow_insecure=_allow_insecure_issuers()
    ):
        raise _idp_unreachable("discovery document issuer mismatch")
    methods = document.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise _idp_unreachable("provider does not support PKCE S256")

    _discovery_cache[issuer] = (document, datetime.now(timezone.utc))
    return document


async def _discovery_endpoint(document: dict, name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise _idp_unreachable(f"discovery document has no {name}")
    await _assert_outbound_url_safe(value)
    return value


async def _discover_authorization_endpoint(issuer_url: str) -> str:
    """Resolve `issuer_url`'s OIDC `authorization_endpoint` (validated)."""
    document = await _get_discovery_document(issuer_url)
    return await _discovery_endpoint(document, "authorization_endpoint")


# --- SSO login: complete --------------------------------------------------------


async def complete_sso_login(
    session: AsyncSession,
    data: SSOCallbackRequest,
    *,
    redirect_uri: str,
    store: sso_transaction.SsoTransactionStore | None = None,
) -> SessionTokens:
    """Complete an SSO login after the IdP has redirected back with a code.

    Everything the callback must prove is checked server-side against the
    transaction `begin_sso_login` created, which is consumed (deleted) first, so
    it works exactly once even if a later step fails:

    1. `redirect_uri` is exactly this organization's callback address;
    2. the transaction exists, is unexpired and unused;
    3. `state`, `org_slug` and `redirect_uri` equal the transaction's;
    4. the organization's SSO is still usable (enabled, not suspended);
    5. the code is exchanged with the stored PKCE verifier, and the ID token's
       signature / issuer / audience / expiry / `nonce` are verified;
    6. the resulting identity is allowed in *now* (`_resolve_or_provision_user`).
    """
    _assert_callback_uri_allowed(redirect_uri, data.org_slug)

    try:
        transaction = await sso_transaction.consume_transaction(
            store or sso_transaction.get_transaction_store(), data.transaction_id
        )
    except sso_transaction.SsoTransactionUnavailableError as exc:
        logger.error("sso_transaction_store_unavailable")
        raise ServiceUnavailableError(
            "Single sign-on is temporarily unavailable. Try again shortly.",
            error_code="auth.sso_unavailable",
        ) from exc
    if transaction is None:
        raise PermissionDeniedError(
            "This sign-in attempt has expired or was already used. Start again.",
            error_code="auth.sso_transaction_invalid",
        )
    if (
        transaction.org_slug != data.org_slug
        or transaction.redirect_uri != redirect_uri
        or not secrets.compare_digest(transaction.state, data.state)
    ):
        logger.warning("sso_callback_state_mismatch")
        raise PermissionDeniedError(
            "This sign-in response does not match the attempt that started it. Start again.",
            error_code="auth.sso_state_mismatch",
        )

    sso_config = await tenancy_service.get_organization_sso_config(session, data.org_slug)

    idp_claims = await _exchange_code_for_claims(
        sso_config,
        code=data.code,
        code_verifier=transaction.code_verifier,
        redirect_uri=redirect_uri,
        expected_nonce=transaction.nonce,
    )

    user_id = await _resolve_or_provision_user(
        session,
        organization_id=sso_config.organization_id,
        idp_subject=idp_claims.sub,
        idp_claims=idp_claims,
    )

    family_id = uuid.uuid4()
    tokens = await _issue_session(
        session, user_id=user_id, organization_id=sso_config.organization_id, family_id=family_id
    )
    logger.info(
        "sso_login_completed",
        user_id=str(user_id),
        organization_id=str(sso_config.organization_id),
    )
    return tokens


# --- Email/password auth: signup + login -----------------------------------------
#
# A second, parallel authentication mechanism alongside SSO -- not a
# replacement for it, and not built by weakening it. Every function above
# this section is untouched. This exists because EKIP's only login path
# until now required a real OIDC identity provider already configured for an
# organization (`sso_configurations`); a self-service user with no IdP of
# their own has had no way to create an account at all. Both functions below
# end by calling the exact same `_issue_session` every SSO login already
# uses, so a password-authenticated session is indistinguishable, to every
# downstream check (REST, MCP, RLS), from an SSO-authenticated one.


async def signup(
    session: AsyncSession, data: SignupRequest
) -> email_verification.PendingDelivery | None:
    """Begin a self-service signup. **Creates nothing that confers access.**

    No organization, user, role or session exists after this call: it only
    records a pending registration (display name, bcrypt hash, organization
    name/slug) and returns the verification mail to send -- or `None` when no
    mail should go out (the address already has an account, or sending is
    rate limited), which the caller must present identically. The account is
    created, as admin of its new organization, by
    `email_verification.verify_token` once the mailbox owner proves control;
    they then sign in through the normal login endpoint.

    The password is hashed before any lookup so the time taken does not
    depend on whether the address is already registered.
    """
    passwords.validate_new_password(data.password, email=data.email)
    password_hash = await asyncio.to_thread(_hash_password, data.password)
    return await email_verification.start_registration(
        session,
        email=data.email,
        display_name=data.display_name,
        password_hash=password_hash,
        organization_name=data.organization_name,
        organization_slug=data.organization_slug,
    )


async def login_with_password(session: AsyncSession, data: LoginRequest) -> LoginResponse:
    """Authenticate with an email + password (`signup`'s counterpart).

    A wrong password and an unknown/SSO-only (no password set) email both
    fail identically -- a generic "invalid credentials" `PermissionDeniedError`
    -- so this endpoint cannot be used to enumerate which emails have an
    account. `CryptContext.verify` itself is timing-safe; the branches above
    it are not constant-time relative to each other, but neither leaks
    anything beyond "invalid," which is the only fact either branch reveals.

    Multi-organization design-gap fix (previously: silently logged into
    whichever organization `resolve_organization_for_login`/
    `get_first_organization_id` happened to return -- both left completely
    unchanged; this now uses the new, additive `list_organizations_for_login`
    instead so it can actually tell "exactly one" apart from "more than
    one"):
      - Zero organizations: unchanged behavior, `auth.no_organization`.
      - Exactly one: unchanged behavior, log straight in with that
        organization -- a single-organization user notices nothing different.
      - More than one: does NOT pick one. Returns
        `OrganizationSelectionRequired` instead of `SessionTokens` -- no
        access token is issued yet, only a short-lived `selection_token`
        that proves this password check already succeeded. The caller must
        then call `select_organization` with a chosen organization_id, which
        independently re-verifies membership before issuing real tokens.
    """
    lookup = await users_service.get_credential_lookup(session, data.email)
    if lookup is None or lookup.password_hash is None:
        # No account / SSO-only account: spend the same work as a real check so response
        # time does not reveal which emails exist.
        await asyncio.to_thread(passwords.verify_against_dummy, data.password)
        raise PermissionDeniedError(
            "Invalid email or password.", error_code="auth.invalid_credentials"
        )
    if not await asyncio.to_thread(_verify_password, data.password, lookup.password_hash):
        raise PermissionDeniedError(
            "Invalid email or password.", error_code="auth.invalid_credentials"
        )
    if not lookup.is_active:
        raise PermissionDeniedError(
            "This account is inactive.", error_code="user.inactive"
        )
    # Only reachable with the CORRECT password, so this reveals nothing to a
    # caller who does not already hold the credential. No access token and
    # no organization-selection token is issued for an unproven address.
    if lookup.email_verified_at is None:
        raise PermissionDeniedError(
            "Your email address has not been verified. Open the confirmation link we "
            "emailed you, or request a new one.",
            error_code="auth.email_not_verified",
        )

    # Upgrade a legacy bcrypt (or weaker-parameter) hash to the current Argon2id one. Only
    # ever after the password was just PROVEN correct, never touches the session cut-off,
    # and is conditional on the stored hash being unchanged (a concurrent reset wins).
    if passwords.needs_rehash(lookup.password_hash):
        upgraded = await asyncio.to_thread(_hash_password, data.password)
        await users_service.upgrade_password_hash(
            session, user_id=lookup.user_id, old_hash=lookup.password_hash, new_hash=upgraded
        )
        logger.info("password_hash_upgraded", user_id=str(lookup.user_id))

    organization_ids, deactivated_ids = await users_service.split_organizations_by_membership(
        session, lookup.user_id
    )
    if len(organization_ids) == 0 and not deactivated_ids:
        raise PermissionDeniedError(
            "This account is not a member of any organization.",
            error_code="auth.no_organization",
        )
    if len(organization_ids) == 0:
        # Correct password, but every membership was deactivated: say so (no session, nothing issued).
        raise membership.membership_deactivated_error()

    # A suspended organization is skipped, never fatal while another is usable: someone who
    # belongs to a suspended AND an active organization signs in to the active one.
    all_rows = await tenancy_repository.get_organizations_by_ids(session, organization_ids)
    organizations = [org for org in all_rows if not is_suspended(org.status)]
    if not organizations:
        first = all_rows[0] if all_rows else None
        raise PermissionDeniedError(
            "Your organization's access has been suspended. Contact your administrator.",
            error_code="auth.organization_suspended",
            detail=(
                {"organization_name": first.name, "customer_message": first.status_message_public}
                if first is not None
                else None
            ),
        )

    if len(organizations) == 1:
        organization_id = organizations[0].id
        tokens = await _issue_session(
            session, user_id=lookup.user_id, organization_id=organization_id, family_id=uuid.uuid4()
        )
        logger.info(
            "password_login_completed",
            user_id=str(lookup.user_id),
            organization_id=str(organization_id),
        )
        return tokens

    selection_token, _selection_expires_at = _issue_org_selection_token(lookup.user_id)
    logger.info(
        "password_login_organization_selection_required",
        user_id=str(lookup.user_id),
        organization_count=len(organizations),
    )
    return OrganizationSelectionRequired(
        selection_token=selection_token,
        organizations=tuple(
            OrganizationSummary(id=org.id, name=org.name, slug=org.slug, status=org.status) for org in organizations
        ),
    )


async def accept_invitation_with_password(
    session: AsyncSession, invitation_id: uuid.UUID, data: InvitationAcceptRequest
) -> email_verification.PendingDelivery | None:
    """Start accepting a password-organization invitation.

    The invitation token is NOT proof of mailbox control: it is returned to
    the *admin* who creates the invitation. The previous implementation
    trusted it as such and (a) adopted any EXISTING account with the invited
    email, (b) overwrote that account's password and (c) issued it a session
    -- an organization admin could take over any EKIP account whose email
    they could name. That is removed:

    * an invitation + token (checked exactly as before) only authorises
      *starting* acceptance;
    * if an account already exists for the address -> 409
      `invitation.account_exists` (nothing is overwritten, nothing issued);
    * otherwise a pending `invitation` verification is recorded with the
      bcrypt hash and a confirmation mail is sent to the invited address. The
      invitation stays `pending`; the user, role and invitation consumption
      happen in `email_verification.verify_token` when the mailbox owner
      proves control. No session is returned -- they sign in afterwards.

    The invitation's organization is resolved through the narrow
    `resolve_invitation_organization` function and tenant context is set
    before any RLS-protected read (this endpoint is unauthenticated, so
    without it `invitations` was unreadable on the real `ekip_app` role).
    """
    organization_id = await repository.resolve_invitation_organization_id(session, invitation_id)
    if organization_id is None:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    await set_tenant_context(session, organization_id)
    # Nobody joins a suspended organization (the invitee holds a valid secret, so naming the
    # reason reveals nothing they do not already know).
    await _assert_organization_not_suspended(session, organization_id)

    invitation = await tenancy_repository.get_invitation_by_id(session, invitation_id)
    if invitation is None:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    if invitation.token_hash is None or not secrets.compare_digest(
        hash_opaque_token(data.token), invitation.token_hash
    ):
        raise PermissionDeniedError(
            "Invalid or missing invitation token.",
            error_code="invitation.invalid_token",
        )
    if invitation.status != "pending":
        raise ConflictError(
            "This invitation is no longer pending.",
            error_code="invitation.not_pending",
            detail={"status": invitation.status},
        )
    if invitation.expires_at <= datetime.now(timezone.utc):
        raise ConflictError(
            "This invitation has expired.",
            error_code="invitation.expired",
        )
    if await users_service.find_user_id_by_email_ci(session, invitation.email) is not None:
        raise ConflictError(
            "An account with this email already exists. Sign in instead.",
            error_code="invitation.account_exists",
        )

    passwords.validate_new_password(data.password, email=invitation.email)
    password_hash = await asyncio.to_thread(_hash_password, data.password)
    delivery = await email_verification.start_invitation_acceptance(
        session,
        invitation_id=invitation.id,
        email=invitation.email,
        display_name=data.display_name or invitation.email,
        password_hash=password_hash,
    )
    logger.info("invitation_acceptance_pending_verification", invitation_id=str(invitation_id))
    return delivery


async def _resolve_client_secret(client_secret_ref: str) -> str:
    """Resolve a stored secret *reference* into the actual usable client
    secret needed for the token exchange.

    `shared/security`'s envelope-encryption helper (PROJECT_PLAN.md section
    12.5 -- a DEK per secret, itself encrypted by a KEK held in a managed
    KMS) now exists and is used elsewhere (`core.tenancy.service.
    register_connector`/`configure_sso`, `core.mcp_oauth.service`) -- this
    function previously predated that module and simply returned
    `client_secret_ref` unchanged, as if it were already the plaintext
    secret, which this module's own docstring flagged loudly rather than
    hid. Now that `configure_sso` envelope-encrypts the client secret at
    write time (see its docstring), this is the matching decrypt-at-read
    half of that same split, mirroring `ingestion.service.
    _execute_ingestion_job`'s identical call for connector credentials.
    """
    return await decrypt_secret(get_kms(), client_secret_ref)


async def _exchange_code_for_claims(
    sso_config: SSOConfiguration,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    expected_nonce: str,
) -> VerifiedIdPClaims:
    """Exchange an authorization code for a verified ID token's claims.

    POSTs to the discovered `token_endpoint` (never following redirects, with a
    timeout, after an SSRF check), then verifies the returned ID token before
    trusting any claim:

    * signing algorithm is on the asymmetric allow-list (`none` / HMAC refused)
      and agrees with the matching JWKS key's type (and its `alg`, if stated);
    * signature (key chosen by `kid`), `aud`, `exp`, `iat` and `sub` present;
    * `iss` equals the configured issuer after normalization (trailing slash);
    * when `aud` lists several audiences, `azp` must be this client;
    * `nonce` equals the one stored in the login transaction (replay / injection
      protection) -- a missing or different nonce is a rejection.

    Network failures, timeouts and malformed provider documents become clean
    `auth.idp_unreachable` errors; a refusal from the token endpoint (expired or
    already-used code) becomes `auth.idp_exchange_failed`.

    Client secrets are decrypted through the shared envelope-encryption helper.
    Group extraction intentionally reads only the standard `groups` claim.
    """
    document = await _get_discovery_document(sso_config.issuer_url)
    token_endpoint = await _discovery_endpoint(document, "token_endpoint")
    jwks_uri = await _discovery_endpoint(document, "jwks_uri")
    client_secret = await _resolve_client_secret(sso_config.client_secret_ref)

    stage = "token"
    try:
        async with _sso_http_client() as client:
            token_response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": sso_config.client_id,
                    "client_secret": client_secret,
                    "code_verifier": code_verifier,
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()

            id_token = token_payload.get("id_token") if isinstance(token_payload, dict) else None
            if not id_token or not isinstance(id_token, str):
                raise PermissionDeniedError(
                    "IdP token response did not include an ID token.",
                    error_code="auth.idp_response_invalid",
                )

            stage = "jwks"
            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if stage == "token" and 400 <= status_code < 500:
            # The provider refused the code (expired, already used, wrong client).
            logger.warning("sso_token_exchange_refused", status=status_code)
            raise PermissionDeniedError(
                "The identity provider rejected this sign-in. Start again.",
                error_code="auth.idp_exchange_failed",
            ) from exc
        raise _idp_unreachable(f"http status {status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise _idp_unreachable(f"request failed: {type(exc).__name__}") from exc

    if not isinstance(jwks, dict):
        raise _idp_unreachable("jwks is not an object")

    try:
        unverified_header = jose_jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise PermissionDeniedError(
            "IdP returned a malformed ID token.",
            error_code="auth.idp_response_invalid",
        ) from exc

    algorithm = unverified_header.get("alg")
    if algorithm not in _ALLOWED_ID_TOKEN_ALGORITHMS:
        logger.warning("sso_id_token_algorithm_rejected", algorithm=str(algorithm)[:16])
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        )

    keys = jwks.get("keys", [])
    matching_key = next(
        (
            key
            for key in (keys if isinstance(keys, list) else [])
            if isinstance(key, dict) and key.get("kid") == unverified_header.get("kid")
        ),
        None,
    )
    if matching_key is None:
        raise PermissionDeniedError(
            "Could not find a matching signing key for this ID token.",
            error_code="auth.idp_key_not_found",
        )
    # The key decides what it may be used for: its type must suit the token's
    # algorithm and, if it states an algorithm, that must be the same one.
    if matching_key.get("kty") not in (None, _KEY_TYPE_FOR_ALGORITHM[algorithm]) or matching_key.get(
        "alg", algorithm
    ) != algorithm:
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        )

    try:
        claims = jose_jwt.decode(
            id_token,
            matching_key,
            algorithms=[algorithm],
            audience=sso_config.client_id,
            options={
                "verify_at_hash": False,
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
                "require_aud": True,
            },
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        ) from exc

    if not issuers_equal(
        claims.get("iss"), sso_config.issuer_url, allow_insecure=_allow_insecure_issuers()
    ):
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        )
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != sso_config.client_id:
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        )
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not secrets.compare_digest(token_nonce, expected_nonce):
        logger.warning("sso_id_token_nonce_mismatch")
        raise PermissionDeniedError(
            "ID token does not belong to this sign-in attempt.",
            error_code="auth.idp_nonce_mismatch",
        )
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise PermissionDeniedError(
            "IdP did not provide a subject claim.",
            error_code="auth.idp_response_invalid",
        )

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise PermissionDeniedError(
            "IdP did not provide an email claim.",
            error_code="auth.idp_response_invalid",
        )

    # An email is only trusted on AFFIRMATIVE evidence of ownership from the
    # provider; a missing, false or wrongly-typed claim is a rejection, never
    # a pass. Evaluated after signature/iss/aud/exp above, which are unchanged.
    verification_source = _idp_email_verification(sso_config.provider, claims)
    if verification_source is None:
        logger.warning(
            "sso_login_rejected_unverified_email",
            organization_id=str(sso_config.organization_id),
            provider=sso_config.provider,
        )
        raise PermissionDeniedError(
            "The identity provider did not verify this email address, so it cannot be "
            "used to sign in or be provisioned.",
            error_code="auth.idp_email_not_verified",
        )

    raw_groups = claims.get("groups")
    groups = tuple(g for g in raw_groups if isinstance(g, str)) if isinstance(raw_groups, list) else ()
    return VerifiedIdPClaims(
        sub=subject,
        email=email,
        name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        groups=groups,
        email_verified=True,
        email_verification_source=verification_source,
    )


def _idp_email_verification(provider: str, claims: dict) -> str | None:
    """Name of the claim that proves the IdP verified `claims["email"]`, or
    None if there is no such proof. Strict: only a JSON boolean `true`
    counts -- `"true"`, `1`, `null`, a missing claim and `false` do not.

    Provider contracts (docs/security/email-verification-audit.md section 3):

    * Google / Google Workspace, Okta, Auth0: the standard OIDC boolean
      `email_verified`.
    * Microsoft Entra ID does not emit `email_verified` by default, and its
      `email` claim is mutable and not guaranteed verified. The only
      equivalent evidence is the optional boolean claim `xms_edov` (email
      domain owner verified), which a tenant must enable on the app
      registration. Without one of the two, an Entra login is rejected rather
      than trusting the address.
    """
    if claims.get("email_verified") is True:
        return "email_verified"
    if provider == "entra_id" and claims.get("xms_edov") is True:
        return "xms_edov"
    return None


async def _resolve_or_provision_user(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    idp_subject: str,
    idp_claims: VerifiedIdPClaims,
) -> uuid.UUID:
    """Resolve an IdP subject to an EKIP user, provisioning both the
    authorization decision and the user itself just-in-time on first login.

    Supersedes the previous "a users row with this email already exists"
    stopgap (ENGINEERING_DECISIONS.md's SSO-provisioning-policy entry).
    "Is this login allowed to join this organization, and with which role" is
    now core/tenancy's `evaluate_provisioning` -- an actual, configurable
    policy decision, not a guess made inside authentication. This function's
    own job is orchestration only: ask tenancy for a decision, and only if
    it's allowed, ask core/users to create/resolve the user and grant the
    role, then record the mapping so this becomes a no-op on the next login.

    The tenant context is set explicitly first: `external_identity_mappings`
    is RLS-protected, and this lookup is the first query of the SSO
    provisioning path -- before `evaluate_provisioning` (which sets the same
    context itself) ever runs. Without it, a returning user's existing
    mapping is invisible (or the query errors under a reused connection's
    empty setting), so the login would try to provision and re-insert the
    mapping instead of resolving it. `organization_id` is the organization
    this login is already committed to, not caller-supplied free input.
    """
    # Defence in depth: `_exchange_code_for_claims` already refuses an
    # unverified email, but nothing downstream (invitation matching, domain
    # rules, account linking) may ever run on one, whatever built `idp_claims`.
    if not idp_claims.email_verified:
        raise PermissionDeniedError(
            "The identity provider did not verify this email address.",
            error_code="auth.idp_email_not_verified",
        )

    await set_tenant_context(session, organization_id)
    mapping = await repository.get_external_identity_mapping(
        session, organization_id, idp_subject
    )
    if mapping is not None:
        state = await users_service.get_user_email_state_by_id(session, mapping.user_id)
        # A deactivated account never gets a session through SSO either (the
        # password login and refresh already refuse it).
        if state is None or not state.is_active:
            logger.warning("sso_login_rejected_inactive_user", organization_id=str(organization_id))
            raise PermissionDeniedError("This account is inactive.", error_code="user.inactive")
        # The IdP may still vouch for this person; THIS organization has deactivated their membership.
        # Refused before anything else happens (and `_issue_session` would refuse again).
        await membership.assert_member_active(session, organization_id, mapping.user_id)
        if await users_service.holds_any_role_in_organization(
            session, user_id=mapping.user_id, organization_id=organization_id
        ):
            await _ensure_mapped_user_email_verified(session, mapping.user_id, idp_claims)
            # This SSO login just PROVED the person behind this IdP subject. If the identity provider has also
            # told us (via SCIM) about an identity with that very externalId, the two are the same person.
            await scim_service.link_pending_identity_for_sso_login(
                session, organization_id, mapping.user_id, idp_subject
            )
            return mapping.user_id
        # The link outlived the membership (the person was removed). A link is
        # not a grant: ignore it and let the organization's CURRENT provisioning
        # rules (invitation / domain / group) decide whether they may come back.
        # If they are admitted the stale link is replaced in this transaction; if
        # they are denied the request rolls back and the (harmless) link stays, so
        # an administrator giving them a role again simply reactivates it.
        logger.warning("sso_stale_identity_mapping_ignored", organization_id=str(organization_id))
        await repository.delete_external_identity_mapping(session, mapping.id)

    decision = await tenancy_service.evaluate_provisioning(
        session,
        organization_id=organization_id,
        email=idp_claims.email,
        groups=idp_claims.groups,
    )
    if not decision.allowed:
        logger.warning(
            "sso_login_not_provisioned",
            organization_id=str(organization_id),
            idp_subject=idp_subject,
            reason=decision.reason,
        )
        raise PermissionDeniedError(
            "This account has not been provisioned. Contact your administrator.",
            error_code="auth.not_provisioned",
            detail={"organization_id": str(organization_id), "reason": decision.reason},
        )

    if decision.grants_role_id is None:
        # Should be unreachable -- ProvisioningDecision's contract guarantees
        # grants_role_id is set whenever allowed=True. Raised explicitly
        # (not via `assert`, which can be stripped under `-O`) so a future
        # bug in evaluate_provisioning fails loudly here, as an unexpected
        # error (AGENT_WORKFLOWS.md's "the system broke" case), rather than
        # surfacing as a confusing downstream foreign-key error.
        raise RuntimeError(
            "evaluate_provisioning returned allowed=True with no grants_role_id."
        )

    # New user (created verified, `via=oidc`), or an EXISTING account only if
    # its own email is already verified -- never linked by email string alone.
    user_id = await users_service.resolve_user_for_sso_provisioning(
        session, email=idp_claims.email, display_name=idp_claims.name or idp_claims.email
    )
    linked_state = await users_service.get_user_email_state_by_id(session, user_id)
    if linked_state is None or not linked_state.is_active:
        raise PermissionDeniedError("This account is inactive.", error_code="user.inactive")
    # A deactivated member is not re-admitted by a matching rule or invitation: an administrator must
    # reactivate them explicitly. (Nothing above this line has been written yet.)
    await membership.assert_member_active(session, organization_id, user_id)
    await users_service.assign_role(
        session,
        user_id=user_id,
        organization_id=organization_id,
        role_id=decision.grants_role_id,
    )

    if decision.matched_invitation_id is not None:
        await tenancy_service.accept_invitation(session, decision.matched_invitation_id)

    await repository.insert_external_identity_mapping(
        session, organization_id=organization_id, user_id=user_id, idp_subject=idp_subject
    )
    await scim_service.link_pending_identity_for_sso_login(session, organization_id, user_id, idp_subject)
    logger.info(
        "sso_user_provisioned",
        user_id=str(user_id),
        organization_id=str(organization_id),
        reason=decision.reason,
    )
    return user_id


async def _ensure_mapped_user_email_verified(
    session: AsyncSession, user_id: uuid.UUID, idp_claims: VerifiedIdPClaims
) -> None:
    """A returning SSO user whose EKIP account predates email verification.

    The IdP has just proven `idp_claims.email`. That is sufficient to mark the
    account verified ONLY when it has no local password (so there is no second
    credential a squatter could hold) and the proven address is the account's
    own. Anything else stays blocked until the owner verifies via the emailed
    link (which also revokes old sessions).
    """
    state = await users_service.get_user_email_state_by_id(session, user_id)
    if state is None or state.email_verified_at is not None:
        return
    if (
        state.is_active
        and not state.has_password
        and state.email.strip().lower() == idp_claims.email.strip().lower()
    ):
        await users_service.mark_email_verified(session, user_id=user_id, via="oidc")
        return
    raise PermissionDeniedError(
        "This account's email address has not been verified. Use the verification email "
        "to verify it, then sign in again.",
        error_code="auth.email_not_verified",
    )


# --- Session issuance, refresh, logout -------------------------------------------


def _hash_token(raw_token: str) -> str:
    """Hash a raw refresh token for storage/lookup (section 12.1: never
    stored in plaintext). SHA-256 is sufficient here -- unlike a password,
    a refresh token is already a high-entropy random value, not something an
    attacker could feasibly brute-force from its hash.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_access_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    scope: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
    org_session_epoch: int = 0,
    member_epoch: int = 0,
) -> tuple[str, datetime, datetime]:
    """Sign a new access token, returning (token, issued_at, expires_at).

    `org_session_epoch` (claim `oe`) is the organization's session epoch at issue time; see
    `resolve_identity` for how it ends pre-suspension tokens for good.

    `scope` (space-separated, RFC 9068 `scope` claim), `audience` (`aud`) and
    `issuer` (`iss`) are set only for tokens issued through the MCP OAuth
    bridge; an ordinary REST session token has none of them. `verify_access_
    token` uses that to keep each kind out of the other's resource.

    `"type": "access"` is new, additive: it lets `verify_access_token`
    reject a well-signed-but-wrong-kind-of token (specifically, an
    org-selection token from `_issue_org_selection_token` below) instead of
    accepting anything bearing a valid signature regardless of what it was
    actually issued for -- the two token kinds share the same signing key,
    so the claim is what tells them apart.
    """
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.jwt_expiry_minutes)
    claims = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "type": "access",
        "iat": int(issued_at.timestamp()),
        # Microsecond-precision issue time. `iat` is whole seconds, which is too coarse to
        # tell "issued just before" from "just after" a password reset in the same second
        # (`users.sessions_valid_after`); `resolve_identity` compares this one instead.
        "iat_us": (issued_at - _EPOCH) // timedelta(microseconds=1),
        "exp": int(expires_at.timestamp()),
        "oe": int(org_session_epoch),
        # Member epoch: moves when THIS person's membership of THIS organization is deactivated or
        # reactivated, ending every token they hold for it (see core.users.membership).
        "me": int(member_epoch),
    }
    if scope:
        claims["scope"] = scope
    if audience:
        claims["aud"] = audience
    if issuer:
        claims["iss"] = issuer
    token = jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, issued_at, expires_at


_ORG_SELECTION_TOKEN_TYPE = "org_selection"


def _issue_org_selection_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    """Sign a short-lived token proving `user_id` already passed a password
    check, for the multi-organization login flow -- returned to the client
    inside `OrganizationSelectionRequired` and presented back to
    `select_organization` alongside the organization the user picked.

    Deliberately NOT an access token: it carries no `organization_id` (none
    has been chosen yet) and is tagged `"type": "org_selection"` rather than
    `"access"`, so `verify_access_token` -- and therefore every endpoint
    behind `CurrentIdentity` -- refuses it even though it is validly signed
    with the same `jwt_secret_key`. Its lifetime (`settings.
    org_selection_token_expiry_minutes`, default 10) is intentionally much
    shorter than a real access token's, since it only needs to survive the
    one extra round trip of the user choosing an organization.
    """
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.org_selection_token_expiry_minutes)
    claims = {
        "sub": str(user_id),
        "type": _ORG_SELECTION_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def _verify_org_selection_token(token: str) -> uuid.UUID:
    """Verify and decode a `selection_token`, returning the `user_id` it was
    issued for. Mirrors `verify_access_token`'s error shape (a generic
    `PermissionDeniedError`, distinct error_code), but checks for
    `"type": "org_selection"` instead of `"type": "access"` -- the two
    verifiers are deliberately not shared, so a bug in one can never
    accidentally relax the other.
    """
    settings = get_settings()
    try:
        claims = jose_jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "Invalid or expired organization selection token.",
            error_code="auth.invalid_selection_token",
        ) from exc

    if claims.get("type") != _ORG_SELECTION_TOKEN_TYPE:
        raise PermissionDeniedError(
            "Invalid organization selection token.",
            error_code="auth.invalid_selection_token",
        )

    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise PermissionDeniedError(
            "Malformed organization selection token.",
            error_code="auth.invalid_selection_token",
        ) from exc


async def _assert_organization_not_suspended(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    """Every way of obtaining a session -- password login, organization selection,
    switching, refresh, SSO, the MCP authorization -- passes through `_issue_session`,
    so a suspended organization is refused here once, for all of them. It is scoped to
    THIS organization: the same person's sessions in other organizations are untouched.
    (`onboarding` is a normal working state and is allowed.)"""
    exists, status, epoch = await tenancy_repository.get_organization_access_state(
        session, organization_id
    )
    if exists and is_suspended(status):
        raise PermissionDeniedError(
            "This organization's access has been suspended. Contact your administrator.",
            error_code="auth.organization_suspended",
            detail=await tenancy_repository.get_organization_suspension_notice(session, organization_id),
        )
    # Returned so `_issue_session` can stamp the new access token with the epoch it was issued under.
    return epoch


async def _issue_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    family_id: uuid.UUID,
    scope: str | None = None,
    oauth_client_id: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> SessionTokens:
    """Issue one access token + one new refresh token, persisting the latter.

    Shared by `complete_sso_login` (a fresh `family_id`) and `refresh`
    (the same `family_id` carried forward from the token being rotated) --
    the only difference between "first login" and "rotation" from this
    function's point of view is which `family_id` the caller passes in.

    Every caller (`complete_sso_login`, `signup`, `login_with_password`,
    `refresh`) reaches this function before any `Identity`-driven dependency
    (`app.api.deps.get_current_identity`) has had a chance to call
    `set_tenant_context` -- there is no request-scoped Identity yet at all
    on a first login/signup, and `refresh` resolves its own organization_id
    from the presented token rather than from an Identity. Milestone 10's
    `refresh_tokens` row is `FORCE ROW LEVEL SECURITY` (`c7d4e8f19a2b`), so
    the INSERT below is itself RLS-checked like any other write to that
    table -- calling `set_tenant_context` here, once, right before it,
    means every caller gets this for free instead of each having to
    remember it independently (a real gap: none of the three
    session-issuing callers set it themselves before this change, which
    surfaced as `asyncpg.exceptions.InvalidTextRepresentationError: invalid
    input syntax for type uuid: ""` the moment the app actually connected
    as the RLS-enforced `ekip_app` role instead of the `BYPASSRLS`
    `neondb_owner` every environment used until now).
    """
    await set_tenant_context(session, organization_id)
    org_epoch = await _assert_organization_not_suspended(session, organization_id)
    # A deactivated member gets no new session by ANY route (login, refresh, selection, switching, SSO, the
    # MCP authorization): this is the one place every session is minted.
    member_epoch = await membership.assert_member_active(session, organization_id, user_id)
    access_token, issued_at, access_expires_at = _issue_access_token(
        user_id,
        organization_id,
        scope=scope,
        audience=audience,
        issuer=issuer,
        org_session_epoch=org_epoch,
        member_epoch=member_epoch,
    )
    raw_refresh_token = secrets.token_urlsafe(48)
    refresh_expires_at = issued_at + _REFRESH_TOKEN_LIFETIME

    await repository.insert_refresh_token(
        session,
        user_id=user_id,
        organization_id=organization_id,
        family_id=family_id,
        token_hash=_hash_token(raw_refresh_token),
        expires_at=refresh_expires_at,
        **({"oauth_client_id": oauth_client_id} if oauth_client_id else {}),
    )

    return SessionTokens(
        access_token=access_token,
        refresh_token=raw_refresh_token,
        expires_in=int((access_expires_at - issued_at).total_seconds()),
    )


async def peek_refresh_token(session: AsyncSession, raw_token: str):
    """Look up a refresh token's owning row without rotating or revoking it.

    Used by `app.mcp.oauth`'s `OAuthAuthorizationServerProvider.load_refresh_token`
    (the MCP-OAuth bridge for Claude's remote connector) to answer "does this
    refresh token exist, and is it still usable" -- the actual rotation and
    reuse-detection still happens in `refresh()` when the token is later
    exchanged, exactly as it would for a REST-originated refresh. Returns
    `None` for the same three reasons `refresh()` itself would reject the
    token (unknown, revoked, expired), just without consuming it.
    """
    token_hash = _hash_token(raw_token)
    token_organization_id = await repository.resolve_refresh_token_organization_id(session, token_hash)
    if token_organization_id is None:
        return None
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


async def _handle_refresh_reuse(
    session: AsyncSession,
    *,
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    revoked_at: datetime,
) -> NoReturn:
    """A refresh token that was already rotated or revoked is being presented again: revoke its WHOLE family,
    durably, and refuse the request. Never returns.

    WHY THIS TAKES OWNERSHIP OF THE TRANSACTION. The request ends in an authentication error, and every
    boundary (`get_db_session`, `session_scope`) rolls the transaction back when one is raised -- so a
    revocation written in that transaction, then followed by the error, is discarded and the family stays
    usable (a stolen, rotated-forward token would keep working). This function therefore ends the request's
    transaction itself, in this order:

      1. refuse for the REAL reason when there is one -- a suspended organization or a deactivated member had
         their tokens revoked on purpose, so this is not a replay: nothing is revoked, nothing rolled back;
      2. `rollback()` -- discards every pending change. Nothing unrelated can be committed with the security
         action (the error being raised would have discarded those changes anyway);
      3. `set_tenant_context` again -- the setting is transaction-local and was just cleared, and
         `refresh_tokens` is FORCE ROW LEVEL SECURITY, so without it the UPDATE would match nothing;
      4. `revoke_family` and `commit()` -- on the SAME session and connection, no savepoint, no second session;
      5. only then the existing `auth.refresh_token_reused` error.

    If the revocation or its commit fails, the database error propagates (the session is rolled back first so
    it is left clean) -- the caller never sees the reuse error for a family that was not actually revoked.
    Callers must treat the session as consumed afterwards. Contains no token value; logs ids only.
    """
    # A suspension revokes every refresh token of the organization. While it lasts, a presenter gets the real
    # reason -- not a "reuse" compromise signal, which this is not. (Raises; nothing below runs.)
    await _assert_organization_not_suspended(session, organization_id)
    # Same for a deactivated member: their refresh tokens were revoked by the deactivation.
    await membership.assert_member_active(session, organization_id, user_id)

    try:
        await session.rollback()
        await set_tenant_context(session, organization_id)
        revoked = await repository.revoke_family(session, family_id, revoked_at=revoked_at)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    logger.warning(
        "refresh_token_reuse_detected",
        family_id=str(family_id),
        user_id=str(user_id),
        organization_id=str(organization_id),
        revoked_tokens=revoked,
    )
    raise PermissionDeniedError("This session has been revoked.", error_code="auth.refresh_token_reused")


async def refresh(
    session: AsyncSession,
    data: RefreshRequest,
    *,
    scope: str | None = None,
    oauth_client_id: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> SessionTokens:
    """Exchange a still-valid refresh token for a new session (rotation).

    `scope`, if given, is carried into the new access token (the MCP OAuth
    bridge passes its scope here); REST callers pass nothing and get exactly
    the token they always did.

    Reuse detection (RefreshToken's model docstring): if the presented token
    has already been revoked (i.e. already rotated away, or already logged
    out), that is treated as a compromise signal -- the entire token family
    is revoked immediately and the request is denied, rather than silently
    accepting a replayed token.

    Milestone 10 RLS note: `refresh_tokens` is RLS-protected, and this
    function starts from a bare, client-presented token hash with no
    `Identity`/org context yet -- the same chicken-and-egg shape
    `ingestion.service._execute_ingestion_job` has for `connector_configs`.
    Resolved the same way: a narrow, RLS-bypassing lookup
    (`repository.resolve_refresh_token_organization_id`) discovers just the
    owning organization_id, `set_tenant_context` is set to it, and only then
    does the real, RLS-scoped `get_refresh_token_by_hash` query run.
    """
    now = datetime.now(timezone.utc)
    token_hash = _hash_token(data.refresh_token)

    token_organization_id = await repository.resolve_refresh_token_organization_id(
        session, token_hash
    )
    if token_organization_id is None:
        raise PermissionDeniedError(
            "Invalid refresh token.", error_code="auth.invalid_refresh_token"
        )
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None:
        raise PermissionDeniedError(
            "Invalid refresh token.", error_code="auth.invalid_refresh_token"
        )

    # A token bound to an OAuth client can only be refreshed BY that client,
    # and an unbound (REST) token can never be refreshed through an OAuth
    # client. A mismatch is a wrong-presenter error, not evidence the token was
    # replayed, so it neither rotates nor revokes anything.
    if row.oauth_client_id != oauth_client_id:
        raise PermissionDeniedError(
            "Invalid refresh token.", error_code="auth.invalid_refresh_token"
        )

    # Plain values, captured now: reuse handling rolls the transaction back, which expires every ORM object
    # loaded in it (an attribute access on `row` afterwards would try to lazy-load inside an async session).
    family_id, user_id, organization_id = row.family_id, row.user_id, row.organization_id

    if row.revoked_at is not None:
        await _handle_refresh_reuse(
            session, family_id=family_id, user_id=user_id, organization_id=organization_id, revoked_at=now
        )

    if row.expires_at <= now:
        raise PermissionDeniedError(
            "Refresh token has expired.", error_code="auth.refresh_token_expired"
        )

    # A refresh token must not outlive the account state that made it
    # acceptable: an inactive or unverified account gets nothing new. Raising
    # here also leaves the token un-rotated (the request transaction rolls back).
    state = await users_service.get_user_email_state_by_id(session, row.user_id)
    if state is None or not state.is_active:
        raise PermissionDeniedError("This account is inactive.", error_code="user.inactive")
    if state.email_verified_at is None:
        raise PermissionDeniedError(
            "This account's email address has not been verified.",
            error_code="auth.email_not_verified",
        )

    # Claim the token with ONE conditional UPDATE (`revoked_at IS NULL`), never a read followed by a write: of
    # several simultaneous presentations of this token exactly one wins; every other one finds it already
    # revoked and is handled as reuse -- so a token can never be rotated into two live descendants.
    if not await repository.revoke_refresh_token_if_active(session, row.id, revoked_at=now):
        await _handle_refresh_reuse(
            session, family_id=family_id, user_id=user_id, organization_id=organization_id, revoked_at=now
        )
    extra: dict[str, str] = {}
    if scope:
        extra["scope"] = scope
    if row.oauth_client_id:
        extra["oauth_client_id"] = row.oauth_client_id
    if audience:
        extra["audience"] = audience
    if issuer:
        extra["issuer"] = issuer
    return await _issue_session(
        session,
        user_id=user_id,
        organization_id=organization_id,
        family_id=family_id,
        **extra,
    )


async def logout(
    session: AsyncSession, data: RefreshRequest, *, oauth_client_id: str | None = None
) -> None:
    """Revoke the single session identified by `data.refresh_token`.

    A token bound to an OAuth client is revoked only when that same client
    presents it (`oauth_client_id`); any other presenter is a silent no-op.

    Idempotent: logging out a token that's already invalid/gone is a no-op,
    not an error -- a client retrying a logout call should never see a
    failure for something that already succeeded.

    Milestone 10 RLS note: same bare-token-hash-before-org-known shape as
    `refresh` above -- see that function's docstring. Here, an unresolvable
    token hash is simply a no-op (matching this function's own idempotent
    contract) rather than an error.
    """
    token_hash = _hash_token(data.refresh_token)

    token_organization_id = await repository.resolve_refresh_token_organization_id(
        session, token_hash
    )
    if token_organization_id is None:
        return
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None or row.revoked_at is not None:
        return
    if row.oauth_client_id != oauth_client_id:
        return
    await repository.revoke_refresh_token(session, row.id, revoked_at=datetime.now(timezone.utc))


async def revoke_all_sessions(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Revoke every active session for `user_id` within `organization_id`
    ("log out everywhere", or admin-forced termination -- section 12.1).

    Returns the number of sessions revoked. Deliberately takes no `actor` /
    permission check yet: whether this may be called on one's own behalf only,
    or by an admin on someone else's behalf, is an authorization question for
    whatever future admin-tooling endpoint calls this -- not decided here.
    """
    return await repository.revoke_all_for_user(
        session, user_id, organization_id, revoked_at=datetime.now(timezone.utc)
    )


async def revoke_organization_sessions(session: AsyncSession, organization_id: uuid.UUID) -> int:
    """End every session ANYONE holds in `organization_id`: all of its refresh-token families
    (including MCP OAuth ones), in one statement. Used when an organization is suspended.

    Scoped to the one organization twice over: the statement filters on it AND runs under that
    organization's tenant context, so row-level security (`refresh_tokens` is FORCE RLS) would
    hide any other organization's rows even if the filter were wrong. The same people's
    sessions in OTHER organizations are untouched. Already-issued access tokens do not need
    revoking: `resolve_identity` refuses them for as long as the organization is suspended.
    Returns the number of refresh tokens revoked.
    """
    await set_tenant_context(session, organization_id)
    revoked = await repository.revoke_all_for_organization(
        session, organization_id, revoked_at=datetime.now(timezone.utc)
    )
    logger.info(
        "organization_sessions_revoked",
        organization_id=str(organization_id),
        refresh_tokens_revoked=revoked,
    )
    return revoked


# --- Access token verification ------------------------------------------------


def _parse_org_epoch(value: object) -> int:
    """The `oe` claim. Absent (a token minted before it existed) is epoch 0 -- the value every
    organization has until its first suspension. A present but malformed value is refused, never
    defaulted: it would otherwise become a way to forge "epoch 0"."""
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("malformed oe claim")


def verify_access_token(
    token: str, *, audience: str | None = None, issuer: str | None = None
) -> TokenClaims:
    """Verify and decode an access token, returning its claims.

    Resource binding: with no `audience`/`issuer` (the REST API) a token that
    carries an `aud` claim -- i.e. one issued to the MCP resource -- is
    REJECTED. With an `audience` and `issuer` (the MCP server) the token must
    carry exactly that `aud` and `iss`, so an ordinary REST session token is
    refused there. Neither kind of token works in the other's resource.

    No database access, by design (mirrors this file's module docstring):
    this answers "whose token is this, and is it validly signed and
    unexpired" -- turning that into a full `Identity` (with roles/permissions
    resolved) is `core.users.service.resolve_identity`'s job, called
    separately by whatever boundary layer (REST or MCP) verified this token.

    The `claims.get("type", "access")` check below is deliberately
    backward-compatible: a token signed before this multi-organization
    change shipped (`_issue_access_token` not yet setting `"type"`) has no
    `type` claim at all, and is treated as `"access"` rather than rejected --
    otherwise every access token already outstanding at deploy time would
    suddenly fail verification. Only a token explicitly tagged some other
    type (currently just `_ORG_SELECTION_TOKEN_TYPE`,
    `"org_selection"`) is rejected here, which is exactly the new case this
    guards against: an `OrganizationSelectionRequired.selection_token`
    presented to an ordinary `CurrentIdentity`-gated endpoint instead of to
    `select_organization`.
    """
    settings = get_settings()
    try:
        claims = jose_jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=audience,
            issuer=issuer,
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "Invalid or expired access token.", error_code="auth.invalid_token"
        ) from exc

    if claims.get("type", "access") != "access":
        raise PermissionDeniedError(
            "This token is not a valid access token.", error_code="auth.invalid_token"
        )

    # `jose` validates `aud`/`iss` only when the claim is present, so a token
    # that simply omits them would pass the decode above. When the caller names
    # a resource it must be bound to it: require the claims to be there.
    if audience is not None:
        token_audience = claims.get("aud")
        if token_audience != audience and not (
            isinstance(token_audience, list) and audience in token_audience
        ):
            raise PermissionDeniedError(
                "This token was not issued for this resource.", error_code="auth.invalid_token"
            )
    if issuer is not None and claims.get("iss") != issuer:
        raise PermissionDeniedError(
            "This token was not issued by the expected issuer.", error_code="auth.invalid_token"
        )

    try:
        iat_us = claims.get("iat_us")
        if isinstance(iat_us, int) and not isinstance(iat_us, bool):
            issued_at = _EPOCH + timedelta(microseconds=iat_us)
        else:
            # Minted before `iat_us` existed: only whole seconds are known, so use the START
            # of that second (the conservative end for a session cut-off check).
            issued_at = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
        return TokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            organization_id=uuid.UUID(claims["organization_id"]),
            issued_at=issued_at,
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
            scopes=tuple(str(claims.get("scope", "")).split()),
            org_session_epoch=_parse_org_epoch(claims.get("oe")),
            member_epoch=_parse_org_epoch(claims.get("me")),
        )
    except (KeyError, ValueError) as exc:
        raise PermissionDeniedError(
            "Malformed access token.", error_code="auth.invalid_token"
        ) from exc


# --- Multi-organization selection/switching ----------------------------------


async def select_organization(session: AsyncSession, data: OrganizationSelectionRequest) -> SessionTokens:
    """Complete a multi-organization login: exchange a `selection_token`
    (from `login_with_password`'s `OrganizationSelectionRequired` response)
    plus a chosen `organization_id` for real `SessionTokens`.

    Security model, per the task's explicit requirement: `data.
    organization_id` is a request, never trusted by itself. The flow is
    `client requests organization X -> backend authenticates current user
    (via the selection_token, which already proved the password check
    succeeded) -> backend independently re-verifies user<->organization X
    membership via list_organizations_for_login -> backend issues a JWT for
    organization X` -- at no point does a client-supplied organization_id
    get written into a token without that membership check running first.
    """
    user_id = _verify_org_selection_token(data.selection_token)
    # The selection token can predate a state change: re-check the account.
    state = await users_service.get_user_email_state_by_id(session, user_id)
    if state is None or not state.is_active:
        raise PermissionDeniedError("This account is inactive.", error_code="user.inactive")
    if state.email_verified_at is None:
        raise PermissionDeniedError(
            "This account's email address has not been verified.",
            error_code="auth.email_not_verified",
        )
    organization_ids, deactivated_ids = await users_service.split_organizations_by_membership(session, user_id)
    if data.organization_id in deactivated_ids:
        raise membership.membership_deactivated_error()
    if data.organization_id not in organization_ids:
        raise PermissionDeniedError(
            "You are not a member of that organization.", error_code="auth.not_a_member"
        )

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=data.organization_id, family_id=uuid.uuid4()
    )
    logger.info(
        "organization_selection_completed",
        user_id=str(user_id),
        organization_id=str(data.organization_id),
    )
    return tokens


async def change_password(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    data: ChangePasswordRequest,
):
    """Change the password of the signed-in user (`POST /auth/password/change`).

    Requires the CURRENT password (a stolen session alone cannot change it), applies the
    same policy as signup and reset, and refuses an unchanged password. Every OTHER
    session -- all refresh tokens in all organizations and every access token issued before
    now -- ends; the current session is replaced by the brand-new one returned here, so the
    caller stays signed in with the new credentials and nothing older keeps working.
    SSO-only accounts (no local password) cannot use this: they have nothing to change and
    must not be handed a second credential.

    Returns `(SessionTokens, PendingPasswordNotice)`.
    """
    from app.core.auth.password_reset import PendingPasswordNotice

    account = await users_service.get_credential_lookup_by_id(session, user_id)
    if account is None or not account.is_active:
        raise PermissionDeniedError("This account is inactive.", error_code="user.inactive")
    if account.password_hash is None:
        raise ValidationError(
            "This account signs in through single sign-on and has no password to change.",
            error_code="auth.no_password",
        )
    if not await asyncio.to_thread(_verify_password, data.current_password, account.password_hash):
        await security_events.audit_user_event(
            session, user_id=user_id, action="user.password_change_failed"
        )
        raise PermissionDeniedError(
            "Your current password is incorrect.", error_code="auth.current_password_incorrect"
        )
    passwords.validate_new_password(data.new_password, email=account.email)
    if await asyncio.to_thread(_verify_password, data.new_password, account.password_hash):
        raise ValidationError(
            "Choose a password different from your current one.",
            error_code="auth.password_unchanged",
        )

    new_hash = await asyncio.to_thread(_hash_password, data.new_password)
    cutoff = datetime.now(timezone.utc)
    await users_service.replace_password(
        session, user_id=user_id, password_hash=new_hash, sessions_valid_after=cutoff
    )
    await repository.consume_open_resets_for_user(session, user_id=user_id, now=cutoff)
    await security_events.revoke_all_sessions_everywhere(
        session, user_id=user_id, revoked_at=cutoff, audit_action="user.password_changed"
    )
    tokens = await _issue_session(
        session, user_id=user_id, organization_id=organization_id, family_id=uuid.uuid4()
    )
    logger.info("password_changed", user_id=str(user_id))
    return tokens, PendingPasswordNotice(email=account.email or "", how="change", when=cutoff)


async def switch_organization(
    session: AsyncSession, *, user_id: uuid.UUID, data: SwitchOrganizationRequest
) -> SessionTokens:
    """Re-scope an already-authenticated session to a different organization
    the same user also belongs to (`POST /auth/switch-organization`).

    `user_id` comes from the caller's *existing*, already-verified
    `CurrentIdentity` (resolved by the router from their current access
    token) -- never from the request body. Same security model as
    `select_organization`: `data.organization_id` is independently
    re-verified via `list_organizations_for_login` before anything is
    issued, so a user cannot switch into an organization by simply naming
    its id. A brand-new access + refresh token pair is issued for the
    target organization (a fresh `family_id`, exactly like a new login)
    rather than mutating the caller's existing token in place -- the old
    token/session for the previous organization is left exactly as it was
    (still valid until its own expiry/logout), it simply does not carry
    access to the newly selected organization, matching the task's
    requirement that switching happen through a newly issued token rather
    than by granting the old one broader scope.
    """
    organization_ids, deactivated_ids = await users_service.split_organizations_by_membership(session, user_id)
    if data.organization_id in deactivated_ids:
        raise membership.membership_deactivated_error()
    if data.organization_id not in organization_ids:
        raise PermissionDeniedError(
            "You are not a member of that organization.", error_code="auth.not_a_member"
        )

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=data.organization_id, family_id=uuid.uuid4()
    )
    logger.info(
        "organization_switch_completed",
        user_id=str(user_id),
        organization_id=str(data.organization_id),
    )
    return tokens


async def list_available_organizations(
    session: AsyncSession, user_id: uuid.UUID
) -> OrganizationsListResponse:
    """List every organization the given (already-authenticated) `user_id`
    belongs to, for `GET /auth/organizations`.

    The router passes `actor.user_id` from `CurrentIdentity` -- the verified
    caller's own id, never a client-supplied one -- so this always answers
    "which organizations does the *caller* belong to," never an arbitrary
    other user's membership.
    """
    organization_ids, _deactivated = await users_service.split_organizations_by_membership(session, user_id)
    organizations = await tenancy_repository.get_organizations_by_ids(session, organization_ids)
    return OrganizationsListResponse(
        organizations=tuple(
            OrganizationSummary(id=org.id, name=org.name, slug=org.slug, status=org.status)
            for org in organizations
            if not is_suspended(org.status)
        )
    )
