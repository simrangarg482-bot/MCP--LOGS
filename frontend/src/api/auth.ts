import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  AuthUser,
  ChangePasswordPayload,
  PasswordResetCompleted,
  PasswordResetRequested,
  InvitationAcceptPayload,
  LoginPayload,
  LoginResponse,
  OrganizationSelectionPayload,
  OrganizationsListResponse,
  SessionTokens,
  SignupPayload,
  SsoCallbackPayload,
  SsoOptions,
  SsoStart,
  SwitchOrganizationPayload,
  VerificationRequested,
  VerifyEmailResult,
} from "@/types/auth";

interface UserProfileResponse {
  id: string;
  email: string;
  displayName: string;
  isActive: boolean;
  roles: string[];
  permissions: string[];
}

const MOCK_USER: AuthUser = {
  id: "user-5",
  name: "Bhawna Relhan",
  email: "bhawna.relhan@navikenz.com",
  organizationId: "org-1",
  role: "owner",
  roles: ["admin"],
  permissions: ["tenancy:manage", "knowledge:review", "observability:read", "incident:write", "postmortem:write", "postmortem:approve", "audit:read"],
};

const MOCK_TOKENS: SessionTokens = {
  accessToken: "mock-access-token",
  refreshToken: "mock-refresh-token",
  tokenType: "bearer",
  expiresIn: 3600,
};

const MOCK_VERIFICATION_REQUESTED: VerificationRequested = {
  status: "verification_sent",
  message: "If this address can be used, a confirmation email is on its way.",
};

/**
 * `POST /auth/signup` (202). Creates NO account and returns NO tokens: the
 * backend only records a pending registration and emails a confirmation
 * link. The account exists once that link has been opened
 * (`verifyEmail`), after which the user signs in normally.
 */
export async function signup(payload: SignupPayload): Promise<VerificationRequested> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_VERIFICATION_REQUESTED, 400);
  }
  return apiRequest<VerificationRequested>("/auth/signup", { method: "POST", body: payload });
}

/**
 * `POST /auth/verify-email`. `token` comes from the URL fragment of the
 * emailed link and is held in memory only (never localStorage/sessionStorage,
 * never logged). Resolves only if the SERVER consumed the token -- the UI
 * must not show success any other way. Returns no session.
 */
export async function verifyEmail(token: string): Promise<VerifyEmailResult> {
  if (USE_MOCK_DATA) {
    return mockDelay({ status: "verified" }, 300);
  }
  return apiRequest<VerifyEmailResult>("/auth/verify-email", { method: "POST", body: { token } });
}

/**
 * `POST /auth/email-verification/resend` (always 202, whether or not anything
 * was sent). Rate limited server-side; a UI cooldown is only a courtesy.
 */
export async function resendVerification(email: string): Promise<VerificationRequested> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_VERIFICATION_REQUESTED, 300);
  }
  return apiRequest<VerificationRequested>("/auth/email-verification/resend", {
    method: "POST",
    body: { email },
  });
}

/**
 * `POST /auth/password/forgot` (always 202). The answer never depends on whether the
 * address has an account, so the UI must show the same message either way.
 */
export async function requestPasswordReset(email: string): Promise<PasswordResetRequested> {
  if (USE_MOCK_DATA) {
    return mockDelay({ status: "reset_requested", message: "ok" }, 400);
  }
  return apiRequest<PasswordResetRequested>("/auth/password/forgot", { method: "POST", body: { email } });
}

/**
 * `POST /auth/password/reset`. `token` comes from the URL FRAGMENT of the emailed link and is
 * held in memory only. Resolves only if the SERVER consumed the token; returns no session --
 * the person signs in with the new password.
 */
export async function resetPassword(token: string, newPassword: string): Promise<PasswordResetCompleted> {
  if (USE_MOCK_DATA) {
    return mockDelay({ status: "password_reset", message: "ok" }, 400);
  }
  return apiRequest<PasswordResetCompleted>("/auth/password/reset", {
    method: "POST",
    body: { token, newPassword },
  });
}

/**
 * `POST /auth/password/change` (authenticated). Returns the NEW session for this organization:
 * every other session -- and the old access token -- is ended by the server, so the caller must
 * replace its stored tokens with this response.
 */
export async function changePassword(payload: ChangePasswordPayload): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 400);
  }
  return apiRequest<SessionTokens>("/auth/password/change", { method: "POST", body: payload });
}

export async function login(payload: LoginPayload): Promise<LoginResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 400);
  }
  // core.auth.schemas.LoginResponse: SessionTokens (status: "complete") for
  // a single-organization user, or OrganizationSelectionRequired (status:
  // "organization_selection_required") for a multi-organization one -- the
  // caller (AuthContext.login) must branch on `.status` before treating
  // this as a real session.
  return apiRequest<LoginResponse>("/auth/login", { method: "POST", body: payload });
}

/**
 * Second step of a multi-organization login: exchange the `selectionToken`
 * an `OrganizationSelectionRequired` login response carried, plus the
 * organization the user picked, for real `SessionTokens`. The backend
 * independently re-verifies membership -- `organizationId` here is a
 * request, not a trusted assertion.
 */
export async function selectOrganization(
  payload: OrganizationSelectionPayload,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 300);
  }
  return apiRequest<SessionTokens>("/auth/select-organization", { method: "POST", body: payload });
}

/**
 * Re-scope the current session to a different organization the user also
 * belongs to (`POST /auth/switch-organization`, bearer-token authenticated).
 * Returns a brand-new access + refresh token pair for the target
 * organization -- callers should replace their stored session with this
 * response, not merge it into the old one.
 */
export async function switchOrganization(
  payload: SwitchOrganizationPayload,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 300);
  }
  return apiRequest<SessionTokens>("/auth/switch-organization", { method: "POST", body: payload });
}

/**
 * Every organization the current caller belongs to (`GET /auth/organizations`),
 * for building an organization switcher. Derived from the caller's own
 * verified identity server-side -- there is no way to ask for another
 * user's memberships through this endpoint.
 */
export async function listMyOrganizations(): Promise<OrganizationsListResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      { organizations: [{ id: "org-1", name: "Navikenz", slug: "navikenz" }] },
      200,
    );
  }
  return apiRequest<OrganizationsListResponse>("/auth/organizations");
}

/**
 * Backend: `POST /invitations/{invitation_id}/accept`. Deliberately
 * unauthenticated -- there is no session yet. `payload.token` (the secret from
 * the invitation link) only authorises STARTING acceptance: it is visible to
 * the inviting admin, so it is not proof of mailbox control. The backend
 * emails a confirmation link to the invited address and finishes the
 * acceptance when that link is opened; no session is returned.
 */
export async function acceptInvitation(
  invitationId: string,
  payload: InvitationAcceptPayload,
): Promise<VerificationRequested> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_VERIFICATION_REQUESTED, 400);
  }
  return apiRequest<VerificationRequested>(`/invitations/${invitationId}/accept`, {
    method: "POST",
    body: payload,
  });
}

/**
 * `GET /auth/{slug}/sso-options` (public, rate limited): may the login page offer SSO for this
 * organization, and with which provider. Never returns an issuer, client id or organization name.
 */
export async function getSsoOptions(slug: string): Promise<SsoOptions> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      slug === "acme"
        ? { available: true, provider: "okta", providerLabel: "Okta" }
        : { available: false, provider: null, providerLabel: null },
      250,
    );
  }
  return apiRequest<SsoOptions>(`/auth/${encodeURIComponent(slug)}/sso-options`);
}

/** `GET /auth/{slug}/login?redirect_uri=...` -- start an SSO login. */
export async function beginSsoLogin(slug: string, redirectUri: string): Promise<SsoStart> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        authorizationUrl: `${redirectUri}?code=mock-code&state=mock-state`,
        state: "mock-state",
        transactionId: "mock-transaction",
      },
      250,
    );
  }
  return apiRequest<SsoStart>(
    `/auth/${encodeURIComponent(slug)}/login?redirect_uri=${encodeURIComponent(redirectUri)}`,
  );
}

/**
 * `POST /auth/callback?redirect_uri=...` -- exchange the provider's code for an EKIP session.
 * The server checks `state` and `transactionId` against its own record (single use); tokens come
 * back in the JSON body, never in a URL.
 */
export async function completeSsoLogin(
  payload: SsoCallbackPayload,
  redirectUri: string,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 300);
  }
  return apiRequest<SessionTokens>(`/auth/callback?redirect_uri=${encodeURIComponent(redirectUri)}`, {
    method: "POST",
    body: payload,
  });
}

export async function refreshSession(refreshToken: string): Promise<SessionTokens> {
  return apiRequest<SessionTokens>("/auth/refresh", {
    method: "POST",
    body: { refreshToken },
  });
}

/**
 * `organizationId` comes from the caller (decoded off the access token's own
 * claims, `tokenStore.decodeAccessTokenClaims`) -- `GET /auth/me` describes
 * the user, not which organization their current session is scoped to.
 */
export async function getCurrentUser(organizationId: string): Promise<AuthUser> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_USER, 150);
  }
  const profile = await apiRequest<UserProfileResponse>("/auth/me");
  return {
    id: profile.id,
    name: profile.displayName,
    email: profile.email,
    organizationId,
    role: (profile.roles[0] as AuthUser["role"]) ?? "member",
    roles: profile.roles,
    permissions: profile.permissions,
  };
}

export async function logout(refreshToken: string): Promise<void> {
  if (USE_MOCK_DATA) {
    return mockDelay(undefined, 100);
  }
  return apiRequest<void>("/auth/logout", { method: "POST", body: { refreshToken } });
}
