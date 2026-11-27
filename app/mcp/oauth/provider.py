"""`EkipOAuthProvider` -- an `OAuthAuthorizationServerProvider` (the `mcp`
package's own OAuth 2.1 / MCP-Authorization-spec interface) that turns
EKIP's *existing* token issuance (`core.auth.service`) into the OAuth
authorization-code + refresh-token flow a spec-compliant remote MCP client
expects.

WHY THIS EXISTS
    Claude's "Add custom connector" UI (the one relevant here; Claude Code's
    CLI `mcp add --header` is a different product surface and already works
    with a static token) offers exactly two authentication options as of
    2026-08: OAuth (Client ID/Secret, with automatic discovery + dynamic
    client registration), or a beta-gated "Request headers" field that is
    not enabled for every account yet. There is no way to paste a static
    `Authorization: Bearer <token>` into this specific UI otherwise. Verified
    against Anthropic's own current documentation (claude.com/docs/connectors/
    custom/remote-mcp), not assumed. So: to make *this* Claude UI work at
    all, EKIP's MCP server must be a real OAuth 2.1 authorization server, per
    the MCP Authorization spec (RFC 8414/9728/7591/7636).

WHY THIS IS A BRIDGE, NOT A NEW AUTH SYSTEM
    EKIP already has exactly one way to mint a real, normally-signed session:
    `core.auth.service._issue_session`/`_issue_access_token`, reached today
    either through a real SSO login (`complete_sso_login`) or, for local/dev
    use with no IdP registered (`scripts/seed_test_organization.py`'s own
    documented, sanctioned pattern), directly. This provider does not add a
    second way to become an EKIP identity -- every token it ever hands back
    to a client is a real token minted by that exact same function. What it
    adds is a *front door* shaped the way OAuth 2.1 requires: `/authorize`,
    `/token`, and dynamic client registration, all of which end up calling
    into the same `core.auth.service` functions the REST API already trusts.
    `app.mcp.auth.resolve_mcp_identity`/`app.mcp.dispatch.run_mcp_tool` are
    completely unchanged -- a token issued through this bridge is
    indistinguishable, to every tool call that follows, from one issued any
    other way.

THE ONE NEW HUMAN STEP: `/authorize`
    OAuth's authorization step needs a human to approve the grant. EKIP has
    no registered SSO provider for its local/dev organizations (`sso_
    configurations` is empty for `test-org`), so there is no third-party IdP
    to redirect to yet -- exactly the situation `seed_test_organization.py`'s
    own docstring already describes and sanctions ("skips only the IdP
    round-trip"). This provider's `/authorize` confirmation page (registered
    as a custom Starlette route, see `register_authorization_confirmation_
    route` below) asks the human for the one piece of proof-of-identity EKIP
    can already verify without a live IdP: an EKIP access token they already
    hold (minted by `seed_test_organization.py`, or by a real SSO login for
    an organization that has one configured). That token is verified for
    real (`core.auth.service.verify_access_token` + `core.users.service.
    resolve_identity` -- the exact same check `app.mcp.auth.resolve_mcp_
    identity` runs for every other MCP request); only then is a *fresh*
    session minted for that same user/organization and handed to the OAuth
    flow. This is deliberately not a one-click "Approve" button with no
    check -- the confirmation URL is reachable over the public ngrok tunnel,
    so a bare click-to-approve would let any internet visitor who finds the
    URL mint themselves a real, tenant-scoped access token. Requiring an
    existing valid token as the credential closes that hole while adding no
    new secret to manage.

    A real production organization with SSO configured should eventually
    have this confirmation step redirect into `begin_sso_login`/
    `complete_sso_login` instead of asking for a pasted token -- deliberately
    NOT built here. Bridging every organization's `/authorize` into its own
    per-org SSO config (there is no org selector in a generic OAuth
    `/authorize` request) is a real, separable feature, flagged here rather
    than either silently skipped or scope-crept into this change.

STORAGE: REGISTERED CLIENTS ARE PERSISTENT; FLOWS/CODES ARE NOT
    Registered OAuth clients (`get_client`/`register_client`) are persisted
    in `oauth_clients` (`app.core.mcp_oauth`, `app.database.models.
    mcp_models.OAuthClient`), not held in process memory. This was NOT the
    original design -- an earlier version of this file kept clients in a
    plain `dict`, on the theory that "a compliant OAuth client re-runs DCR
    transparently the next time its remembered client_id is rejected" would
    make a restart harmless. That theory was falsified by an actual
    production-shaped observation, not a hypothetical: during this
    project's own Claude remote-connector integration testing, the MCP
    server was restarted several times (each carrying a real bug fix)
    while Claude held onto a `client_id`/`client_secret`/refresh token it
    had legitimately cached hours earlier. Claude did NOT silently
    re-register -- its cached refresh-token exchange failed outright with
    `401 invalid_client`, surfacing to the user as a broken connector. A
    real deployment restarts far less often than this dev session did, but
    it still restarts (deploys, crashes, autoscaling) -- and every one of
    Claude's already-connected users would hit this same failure
    simultaneously. Persisting the client registry is what makes "Claude
    stays connected across a server restart" actually true rather than
    "usually recovers on its own."

    Pending authorization flows (`_pending_flows`) and issued-but-not-yet-
    exchanged authorization codes (`_auth_codes`/`_issued_sessions`)
    remain in-memory, deliberately: both are short-lived (5-10 minutes,
    single-use) and mid-flight by nature -- a restart during the few
    seconds between `/authorize` and the human clicking "Authorize" simply
    fails that one in-progress attempt, the same as restarting any web
    server mid-request would. There is no long-lived credential to lose
    here the way there is for a registered client. Access and refresh
    tokens themselves are, as before, NOT stored here at all -- they are
    the real, DB-persisted EKIP tokens `core.auth.service` already owns, so
    a restart never invalidates a session a user is actively relying on.
"""

from __future__ import annotations

import hmac
import html
import secrets
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest
from app.core.exceptions import EKIPError
from app.core.mcp_oauth import service as mcp_oauth_service
from app.core.users import service as users_service
from app.mcp.oauth.constants import (
    MCP_SCOPE,
    accepted_resource_indicators,
    mcp_resource_identifier,
)
from app.mcp.oauth.redirect_policy import first_redirect_uri_violation
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)

#: How long an `/authorize` redirect (flow_id) or an issued authorization
#: code stays valid before it must be restarted. Short-lived by design --
#: both are single-use, and the human step in between is expected to take
#: seconds, not minutes.
_FLOW_TTL_SECONDS = 600
_CODE_TTL_SECONDS = 300

#: Rolling window over which `mcp_oauth_registrations_per_day` is counted.
_REGISTRATION_WINDOW_SECONDS = 24 * 3600


@dataclass
class _PendingFlow:
    client_id: str
    params: AuthorizationParams
    client_name: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    error: str | None = None
    #: Browser binding for the consent form (see `_handle_confirmation_post`):
    #: delivered as an HttpOnly cookie on the GET and echoed in a hidden field.
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))


def _harden_page(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"


def _expired_page() -> Response:
    response = HTMLResponse(
        "<p>This authorization link has expired or was already used. "
        "Return to Claude and try connecting again.</p>",
        status_code=400,
    )
    _harden_page(response)
    return response


def _csrf_failed_page() -> Response:
    response = HTMLResponse(
        "<p>This request could not be verified as coming from the authorization page "
        "you opened. Reload that page, or return to Claude and try connecting again.</p>",
        status_code=403,
    )
    _harden_page(response)
    return response


class EkipOAuthProvider:
    """See module docstring. Reads `app.mcp.servers.server.session_factory`/
    `set_tenant_context` lazily (module attribute lookup at call time, not a
    constructor argument) -- the same dependency-inversion trick that
    module's own docstring documents for `app.mcp.dispatch.run_mcp_tool`,
    needed here for the identical reason: this object is constructed at
    `app.mcp.servers.server` import time, before `scripts/run_mcp_server.py`
    has injected the real database session factory.
    """

    def __init__(self) -> None:
        self._pending_flows: dict[str, _PendingFlow] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        #: code -> the real `SessionTokens` minted for it, popped (single
        #: use) on exchange. Never touched again after that -- there is
        #: nothing here for a restart to leak, since it is DB-issued, not
        #: forged.
        self._issued_sessions: dict[str, auth_service.SessionTokens] = {}

    # -- dependencies, resolved lazily (see class docstring) -----------------

    @staticmethod
    def _session_factory():
        from app.mcp.servers import server as server_module

        if server_module.session_factory is None:
            raise RuntimeError(
                "app.mcp.servers.server.session_factory is unset -- "
                "scripts/run_mcp_server.py must set it before serving requests."
            )
        return server_module.session_factory

    @staticmethod
    def _set_tenant_context():
        from app.mcp.servers import server as server_module

        if server_module.set_tenant_context is None:
            raise RuntimeError(
                "app.mcp.servers.server.set_tenant_context is unset -- "
                "scripts/run_mcp_server.py must set it before serving requests."
            )
        return server_module.set_tenant_context

    # -- dynamic client registration (RFC 7591) -------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        session_factory = self._session_factory()
        async with session_factory() as session:
            data = await mcp_oauth_service.get_registered_client(session, client_id)
        if data is None:
            return None
        client = OAuthClientInformationFull.model_validate(data)
        # A client registered before the redirect-URI policy existed (or with a
        # since-removed host) is treated as unknown: the SDK then answers with
        # a direct error and never redirects anywhere.
        if first_redirect_uri_violation(
            client.redirect_uris or [], get_settings().mcp_oauth_allowed_redirect_hosts
        ):
            logger.warning("mcp_oauth_client_redirect_uri_not_allowed", client_id=client_id)
            return None
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        violation = first_redirect_uri_violation(
            client_info.redirect_uris or [], get_settings().mcp_oauth_allowed_redirect_hosts
        )
        if violation:
            logger.warning("mcp_oauth_registration_rejected", reason=violation)
            raise RegistrationError(error="invalid_redirect_uri", error_description=violation)
        session_factory = self._session_factory()
        capped = False
        async with session_factory() as session:
            recent = await mcp_oauth_service.count_recent_registrations(
                session, within_seconds=_REGISTRATION_WINDOW_SECONDS
            )
            if recent >= get_settings().mcp_oauth_registrations_per_day:
                capped = True
                logger.warning("mcp_oauth_registration_cap_reached", recent=recent)
            else:
                await mcp_oauth_service.register_oauth_client(
                    session, client_info.model_dump(mode="json")
                )
        if capped:
            # Raised outside the `async with`: the SDK's `RegistrationError` is a
            # frozen dataclass, and an exception propagating through the session
            # context manager has its `__traceback__` assigned (FrozenInstanceError).
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Client registration is temporarily unavailable; retry later.",
            )
        logger.info("mcp_oauth_client_registered", client_id=client_info.client_id)

    # -- /authorize ------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Stash the request and hand back a URL to EKIP's own confirmation
        page (`register_authorization_confirmation_route`'s route) instead of
        a third-party IdP -- see module docstring for why there is no
        third-party IdP to redirect to yet.
        """
        if params.resource is not None and params.resource not in accepted_resource_indicators():
            # RFC 8707 section 2: a token can only be requested for a resource
            # this server is. Refused here, before any flow or code exists.
            raise AuthorizeError(
                error="invalid_target",
                error_description="The requested resource is not served by this authorization server.",
            )
        flow_id = uuid.uuid4().hex
        self._pending_flows[flow_id] = _PendingFlow(
            client_id=client.client_id, params=params, client_name=client.client_name
        )
        return f"/ekip/oauth/authorize?flow_id={flow_id}"

    def _pop_expired(self) -> None:
        """Sweep out stale pending flows.

        `_handle_confirmation_get`/`_handle_confirmation_post` both call this
        first, on every request -- including concurrent ones for genuinely
        different `flow_id`s. Two such calls can each build an `expired` list
        containing the SAME long-abandoned `flow_id` (there is no `await`
        between building that list and deleting from it, but there easily is
        one *across* two separate incoming requests racing each other), and
        whichever one's `del` ran second used to raise `KeyError` on an
        already-removed key -- the second, previously unfixed instance of the
        exact same non-atomic-delete bug `_handle_confirmation_post`'s own
        fix (see its comment) already closed for the claim-the-flow step.
        `dict.pop(key, None)` makes removing an already-gone key a no-op
        instead of an error, which is all "sweep out anything stale" ever
        needed in the first place.
        """
        now = time.monotonic()
        expired = [
            flow_id
            for flow_id, flow in self._pending_flows.items()
            if now - flow.created_at > _FLOW_TTL_SECONDS
        ]
        for flow_id in expired:
            self._pending_flows.pop(flow_id, None)

    @staticmethod
    def _csrf_cookie_name(flow_id: str) -> str:
        # One cookie per flow, so two authorizations open in one browser
        # never overwrite each other's binding.
        return f"ekip_oauth_csrf_{flow_id}"

    async def _render_confirmation_page(self, flow_id: str, flow: _PendingFlow) -> Response:
        # Every interpolated value is HTML-escaped: client name, redirect URI
        # and the error text are all attacker- or exception-influenced.
        esc = html.escape
        redirect_uri = str(flow.params.redirect_uri)
        redirect_host = urlsplit(redirect_uri).netloc or redirect_uri
        scopes = " ".join(flow.params.scopes or [MCP_SCOPE])
        client_name = flow.client_name or "an application that did not give a name"
        error_html = f'<p class="error">{esc(flow.error)}</p>' if flow.error else ""
        response = HTMLResponse(
            f"""<!doctype html>
<html><head><title>Authorize EKIP MCP access</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; max-width: 34rem; margin: 3rem auto;
          color: #1e293b; padding: 0 1rem; }}
  h1 {{ font-size: 1.25rem; }}
  dl {{ background: #f1f5f9; border-radius: 0.5rem; padding: 0.75rem 1rem; }}
  dt {{ color: #64748b; font-size: 0.8rem; margin-top: 0.5rem; }}
  dd {{ margin: 0; font-family: monospace; word-break: break-all; }}
  dd.host {{ font-weight: 700; font-size: 1.05rem; }}
  p.hint {{ color: #64748b; font-size: 0.9rem; }}
  p.warn {{ color: #92400e; font-size: 0.9rem; }}
  p.error {{ color: #b91c1c; font-weight: 600; }}
  textarea {{ width: 100%; height: 6rem; font-family: monospace; box-sizing: border-box; }}
  button {{ margin-top: 1rem; padding: 0.5rem 1.25rem; background: #2563eb; color: white;
            border: none; border-radius: 0.25rem; cursor: pointer; font-size: 1rem; }}
</style></head>
<body>
  <h1>Authorize this client to use your EKIP identity</h1>
  <dl>
    <dt>Application requesting access</dt><dd>{esc(client_name)}</dd>
    <dt>Client ID</dt><dd>{esc(flow.client_id)}</dd>
    <dt>You will be sent back to</dt><dd class="host">{esc(redirect_host)}</dd>
    <dt>Full redirect address</dt><dd>{esc(redirect_uri)}</dd>
    <dt>Access requested</dt><dd>{esc(scopes)}</dd>
  </dl>
  <p class="warn">
    Only continue if you started this connection yourself and recognise the
    address above. Anyone who receives the access you grant can use EKIP as you.
  </p>
  <p class="hint">
    Paste a valid EKIP access token to continue. Mint one with
    <code>python scripts/seed_test_organization.py</code> (development), or via a
    real SSO login for an organization that has one configured. This proves who
    you are once; EKIP then issues this client its own, independently refreshable
    session -- it does not reuse the token you paste beyond this step.
  </p>
  {error_html}
  <form method="post" action="/ekip/oauth/authorize">
    <input type="hidden" name="flow_id" value="{esc(flow_id)}">
    <input type="hidden" name="csrf_token" value="{esc(flow.csrf_token)}">
    <textarea name="ekip_access_token" placeholder="eyJhbGciOi..." required></textarea>
    <button type="submit">Authorize</button>
  </form>
</body></html>"""
        )
        response.set_cookie(
            self._csrf_cookie_name(flow_id),
            flow.csrf_token,
            max_age=_FLOW_TTL_SECONDS,
            path="/ekip/oauth/authorize",
            httponly=True,
            samesite="lax",
            secure=get_settings().mcp_public_base_url.lower().startswith("https://"),
        )
        _harden_page(response)
        return response

    async def _handle_confirmation_get(self, request: Request) -> Response:
        self._pop_expired()
        flow_id = request.query_params.get("flow_id", "")
        flow = self._pending_flows.get(flow_id)
        if flow is None:
            return _expired_page()
        return await self._render_confirmation_page(flow_id, flow)

    def _csrf_valid(self, request: Request, form, flow_id: str, flow: _PendingFlow) -> bool:
        """The form value, the cookie, and the flow's own stored token must all
        match (constant-time) -- i.e. this POST came from the browser that
        loaded this flow's consent page."""
        expected = flow.csrf_token.encode()
        submitted = str(form.get("csrf_token", "")).encode()
        cookie = request.cookies.get(self._csrf_cookie_name(flow_id), "").encode()
        return hmac.compare_digest(submitted, expected) and hmac.compare_digest(cookie, expected)

    async def _handle_confirmation_post(self, request: Request) -> Response:
        form = await request.form()
        flow_id = str(form.get("flow_id", ""))
        raw_token = str(form.get("ekip_access_token", "")).strip()

        # Claim the flow atomically (a single `dict.pop`, no `await` between
        # the lookup and the removal) instead of a separate `get` followed by
        # a later `del`. A browser can genuinely submit this form twice for
        # the same `flow_id` -- a double-click before the button disables,
        # or a retried request -- and under the old get-then-later-del shape
        # both concurrent requests would pass the "does this flow exist"
        # check, both would run the full token-verify + session-issuance
        # work, and whichever `del` ran second would raise `KeyError` on an
        # already-removed key (an *uncaught* exception -- a real 500 this
        # module's own dev-token confirmation page hit in practice, not a
        # hypothetical). Popping up front means only one concurrent request
        # can ever claim the flow; every other one gets the same clean
        # "already used" response immediately, before doing any DB work.
        self._pop_expired()
        # The browser binding is checked BEFORE the flow is consumed, so a
        # forged or cross-site POST can neither succeed nor burn the victim's
        # pending authorization. No `await` separates the lookup from the pop
        # below, so only one concurrent submission can still claim the flow.
        flow = self._pending_flows.get(flow_id)
        if flow is None:
            return _expired_page()
        if not self._csrf_valid(request, form, flow_id, flow):
            logger.warning("mcp_oauth_csrf_check_failed", client_id=flow.client_id)
            return _csrf_failed_page()
        self._pending_flows.pop(flow_id, None)

        granted_scopes = flow.params.scopes or [MCP_SCOPE]
        try:
            claims = auth_service.verify_access_token(raw_token)
            session_factory = self._session_factory()
            set_tenant_context = self._set_tenant_context()
            async with session_factory() as session:
                identity = await users_service.resolve_identity(
                    session,
                    claims.user_id,
                    claims.organization_id,
                    token_issued_at=claims.issued_at,
                    token_org_epoch=claims.org_session_epoch,
                    token_member_epoch=claims.member_epoch,
                )
                await set_tenant_context(session, identity.organization_id)
                tokens = await auth_service._issue_session(
                    session,
                    user_id=identity.user_id,
                    organization_id=identity.organization_id,
                    family_id=uuid.uuid4(),
                    scope=" ".join(granted_scopes),
                    oauth_client_id=flow.client_id,
                    audience=mcp_resource_identifier(),
                    issuer=mcp_resource_identifier(),
                )
        except EKIPError as exc:
            # An invalid token is a mistake the human can fix (wrong paste,
            # expired token) -- put the flow back so the same link still
            # works for a retry, rather than making a typo cost them the
            # whole `/authorize` round-trip with Claude.
            flow.error = f"That token could not be verified: {exc.message}"
            self._pending_flows[flow_id] = flow
            return await self._render_confirmation_page(flow_id, flow)

        code = uuid.uuid4().hex + uuid.uuid4().hex  # >128 bits entropy, per RFC 6749 section 10.10
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=granted_scopes,
            expires_at=time.time() + _CODE_TTL_SECONDS,
            client_id=flow.client_id,
            code_challenge=flow.params.code_challenge,
            redirect_uri=flow.params.redirect_uri,
            redirect_uri_provided_explicitly=flow.params.redirect_uri_provided_explicitly,
            resource=flow.params.resource,
            subject=str(identity.user_id),
        )
        self._issued_sessions[code] = tokens
        logger.info(
            "mcp_oauth_authorization_granted",
            client_id=flow.client_id,
            organization_id=str(identity.organization_id),
        )
        redirect_url = construct_redirect_uri(str(flow.params.redirect_uri), code=code, state=flow.params.state)
        return RedirectResponse(url=redirect_url, status_code=302)

    # -- /token: authorization_code grant --------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        entry = self._auth_codes.get(authorization_code)
        if entry is None or entry.client_id != client.client_id:
            return None
        if entry.expires_at < time.time():
            # `.pop(..., None)`, not `del` -- same non-atomic-delete race as
            # `_pop_expired`/`_handle_confirmation_post` (see their comments):
            # a concurrent duplicate call for this same expired code would
            # otherwise raise `KeyError` on the second `del`.
            self._auth_codes.pop(authorization_code, None)
            self._issued_sessions.pop(authorization_code, None)
            return None
        return entry

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        tokens = self._issued_sessions.pop(authorization_code.code, None)
        self._auth_codes.pop(authorization_code.code, None)  # single-use
        if tokens is None:
            raise TokenError(error="invalid_grant", error_description="Authorization code already used or expired.")
        return OAuthToken(
            access_token=tokens.access_token,
            token_type="Bearer",
            expires_in=tokens.expires_in,
            scope=" ".join(authorization_code.scopes),
            refresh_token=tokens.refresh_token,
        )

    # -- /token: refresh_token grant --------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        session_factory = self._session_factory()
        async with session_factory() as session:
            row = await auth_service.peek_refresh_token(session, refresh_token)
        # Only a token issued TO an OAuth client can be presented by one. Its
        # `client_id` is the client it was issued to (not the presenter), so the
        # SDK's own `refresh_token.client_id != request client_id` check, the
        # revocation endpoint's ownership check, and `auth_service.refresh`'s
        # binding check all agree that another client cannot use it.
        if row is None or row.oauth_client_id is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.oauth_client_id,
            scopes=[MCP_SCOPE],
            expires_at=int(row.expires_at.timestamp()),
            subject=str(row.user_id),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        session_factory = self._session_factory()
        try:
            async with session_factory() as session:
                tokens = await auth_service.refresh(
                    session,
                    RefreshRequest(refresh_token=refresh_token.token),
                    scope=" ".join(scopes or refresh_token.scopes),
                    oauth_client_id=client.client_id,
                    audience=mcp_resource_identifier(),
                    issuer=mcp_resource_identifier(),
                )
        except EKIPError as exc:
            raise TokenError(error="invalid_grant", error_description=exc.message) from exc
        return OAuthToken(
            access_token=tokens.access_token,
            token_type="Bearer",
            expires_in=tokens.expires_in,
            scope=" ".join(scopes or refresh_token.scopes),
            refresh_token=tokens.refresh_token,
        )

    # -- resource-server side: verifying a presented access token ---------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Transport-level gate only -- "is this a well-formed, unexpired EKIP
        token", answered the same way `app.mcp.auth.resolve_mcp_identity`
        answers it (`verify_access_token`, DB-free by design). This does NOT
        replace `app.mcp.dispatch.run_mcp_tool`'s own identity/RLS resolution,
        which still runs, unchanged, for every tool call -- this is what lets
        the `mcp` package's bearer-auth middleware issue a proper
        `401 + WWW-Authenticate` challenge (which is what makes Claude's
        client attempt OAuth discovery in the first place) instead of the
        request reaching tool dispatch unauthenticated.
        """
        try:
            resource = mcp_resource_identifier()
            claims = auth_service.verify_access_token(token, audience=resource, issuer=resource)
        except EKIPError:
            return None
        return AccessToken(
            token=token,
            client_id="ekip-resource",
            scopes=list(claims.scopes),
            expires_at=int(claims.expires_at.timestamp()),
            resource=resource,
            subject=str(claims.user_id),
            claims={"organization_id": str(claims.organization_id), "iss": resource},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if not isinstance(token, RefreshToken):
            return  # access tokens are short-lived JWTs; nothing stateful to revoke
        session_factory = self._session_factory()
        async with session_factory() as session:
            await auth_service.logout(
                session,
                RefreshRequest(refresh_token=token.token),
                oauth_client_id=token.client_id,
            )


def register_authorization_confirmation_route(mcp_server, provider: EkipOAuthProvider) -> None:
    """Attach `EkipOAuthProvider`'s human-facing `/ekip/oauth/authorize`
    confirmation page to `mcp_server` as a plain Starlette route. Kept
    separate from the provider class (rather than a decorator inside
    `__init__`) so this module never imports `app.mcp.servers.server` at
    module load time -- `server.py` imports *this* module to construct the
    provider, so the reverse import would be circular.
    """

    @mcp_server.custom_route("/ekip/oauth/authorize", methods=["GET"], include_in_schema=False)
    async def _confirm_get(request: Request) -> Response:
        return await provider._handle_confirmation_get(request)

    @mcp_server.custom_route("/ekip/oauth/authorize", methods=["POST"], include_in_schema=False)
    async def _confirm_post(request: Request) -> Response:
        return await provider._handle_confirmation_post(request)
