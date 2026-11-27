export interface AuthUser {
  id: string;
  name: string;
  email: string;
  avatarUrl?: string;
  organizationId: string;
  // First role name only (kept for existing call sites); custom roles mean this
  // is any role name, not just the four literals.
  role: "owner" | "admin" | "member" | "viewer";
  // Every role name the user holds in the current organization (`GET /auth/me`).
  roles: string[];
  // Flattened permission codes from `GET /auth/me` (`core.users.schemas.
  // UserProfile.permissions`) -- the same set `Identity.permissions`
  // enforces server-side. The frontend uses this only to hide/disable
  // actions for UX; the backend remains authoritative and re-checks every
  // request regardless of what this array says.
  permissions: string[];
}

export interface SessionTokens {
  // Additive (core.auth.schemas.SessionTokens.status), always "complete"
  // for this shape -- present so a discriminated `LoginResponse` union can
  // tell this apart from `OrganizationSelectionRequired` below without a
  // separate type guard. Optional here (rather than a required literal)
  // so any pre-existing frontend code building a `SessionTokens` object by
  // hand (tests, mocks) doesn't need updating just to satisfy the type.
  status?: "complete";
  accessToken: string;
  refreshToken: string;
  tokenType: "bearer";
  expiresIn: number;
}

// Multi-organization login/session design gap fix: a password login no
// longer silently picks an organization for a user who belongs to more
// than one -- see core.auth.service.login_with_password's docstring.
export interface OrganizationSummary {
  id: string;
  name: string;
  slug: string;
  status?: "onboarding" | "active" | "suspended";
}

export interface OrganizationSelectionRequired {
  status: "organization_selection_required";
  // Proves the password check already succeeded; short-lived (backend
  // default 10 minutes, Settings.org_selection_token_expiry_minutes).
  // Presented back to POST /auth/select-organization along with the
  // chosen organizationId -- never usable as a bearer access token
  // (core.auth.service.verify_access_token rejects it by its "type" claim).
  selectionToken: string;
  organizations: OrganizationSummary[];
}

// What POST /auth/login actually returns now (core.auth.schemas.LoginResponse):
// SessionTokens for a single-organization user (unchanged), or
// OrganizationSelectionRequired for a multi-organization one. Callers must
// branch on `.status` before treating the result as a session.
export type LoginResponse = SessionTokens | OrganizationSelectionRequired;

/** `GET /auth/{slug}/sso-options` -- deliberately minimal and identical for an unknown
 * organization, one without SSO, one with SSO disabled and a suspended one. */
export interface SsoOptions {
  available: boolean;
  provider: "entra_id" | "okta" | "auth0" | "google_workspace" | null;
  providerLabel: string | null;
}

/** `GET /auth/{slug}/login` -- the provider URL plus an opaque, single-use handle to the
 * server-side transaction. The PKCE verifier and nonce never reach the browser. */
export interface SsoStart {
  authorizationUrl: string;
  state: string;
  transactionId: string;
}

/** `POST /auth/password/forgot` -- identical whether or not the address has an account. */
export interface PasswordResetRequested {
  status: "reset_requested";
  message: string;
}

export interface PasswordResetCompleted {
  status: "password_reset";
  message: string;
}

export interface ChangePasswordPayload {
  currentPassword: string;
  newPassword: string;
}

export interface SsoCallbackPayload {
  orgSlug: string;
  code: string;
  state: string;
  transactionId: string;
}

export interface OrganizationSelectionPayload {
  selectionToken: string;
  organizationId: string;
}

export interface SwitchOrganizationPayload {
  organizationId: string;
}

export interface OrganizationsListResponse {
  organizations: OrganizationSummary[];
}

export interface SignupPayload {
  email: string;
  password: string;
  displayName: string;
  organizationName: string;
  organizationSlug: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface InvitationAcceptPayload {
  token: string;
  password: string;
  displayName?: string;
}

// Email verification (docs/security/email-verification-*.md). The backend is
// the only source of truth: nothing below is ever treated as proof of
// verification until a `verifyEmail` call actually resolved successfully.

// Generic acknowledgement for signup, invitation acceptance and resend. It is
// deliberately identical whether or not a message was really sent (so the
// endpoints cannot be used to enumerate accounts) -- never infer from it that
// an address is registered or unregistered.
export interface VerificationRequested {
  status: "verification_sent";
  message: string;
}

export interface VerifyEmailResult {
  status: "verified";
}

// `ApiError.errorCode` values the verification UI branches on.
export type EmailVerificationErrorCode =
  | "email_verification.invalid"
  | "email_verification.expired"
  | "email_verification.already_used"
  | "auth.email_not_verified"
  | "invitation.account_exists"
  | "email.not_configured";
