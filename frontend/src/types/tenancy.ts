import type { UUID } from "./common";

export interface Organization {
  id: UUID;
  name: string;
  slug: string;
  /** Read-only. Only a platform operator can change it (there is no tenant API for that). */
  status?: "onboarding" | "active" | "suspended";
}

export interface Project {
  id: UUID;
  organizationId: UUID;
  name: string;
  isDefault: boolean;
  createdAt: string;
}

export type UserRole = "owner" | "admin" | "member" | "viewer";

export interface OrgUser {
  id: UUID;
  name: string;
  email: string;
  role: UserRole;
  /** Every role name the member holds (custom role names are arbitrary). */
  roles: string[];
  /** The same roles as ids, for the role editor. */
  roleIds: UUID[];
  status: "active" | "invited" | "suspended";
  /** This organization's access state for the member (never the global account flag). */
  membershipStatus?: "active" | "deactivated";
  /** Who deactivated them: an administrator or the identity provider (SCIM). */
  membershipSource?: "admin" | "scim" | null;
  lastActiveAt?: string;
}

/** `grantsRole` is a role *name* (e.g. "engineer") -- one of the roles listed by
 * `GET /organizations/{id}/roles` (the built-in `admin` or one of this
 * organization's own roles); the invite form offers them as a dropdown. */
export interface InvitationRequest {
  email: string;
  grantsRole: string;
}

/** Delivery of the invitation email (`core.tenancy.schemas.InvitationEmailStatus`).
 * `sent` means the mail server accepted the message; `queued` means a background
 * task still owes the send; `not_configured` means the server has no email
 * provider, so the link must be copied by hand. */
export type InvitationEmailStatus = "not_sent" | "queued" | "sent" | "failed" | "not_configured";

export interface Invitation {
  id: UUID;
  organizationId: UUID;
  email: string;
  status: "pending" | "accepted" | "expired" | "revoked";
  expiresAt: string;
  emailStatus: InvitationEmailStatus;
  emailAttempts: number;
  emailSentAt?: string | null;
  resendCount: number;
  /** The single-use secret proving control of `email` (`AcceptInvitationPage`
   * needs this, not just `id`) -- present ONLY on the response to the
   * `create_invitation` call that generated it (`core.tenancy.schemas.
   * Invitation.token`'s own docstring); every other read of an invitation
   * (e.g. `listInvitations`) gets `token: undefined`, since the backend
   * never stores or re-exposes the raw value. There is no email-sending in
   * this codebase (docs/USER_TESTING_GUIDE.md section 3), so this is the
   * only place the accept link can be obtained from -- copy it out before
   * navigating away. */
  token?: string;
}

/** Mirrors `app.core.tenancy.schemas.AccessRuleType` exactly -- the real
 * backend rejects anything else with a 422. There is no "email" rule type;
 * that need is served by `Invitation` instead (per that schema's own
 * docstring). */
export type AccessRuleType = "domain" | "group";

/** Mirrors `app.core.tenancy.schemas.AccessRuleCreate`. `grantsRole` is a
 * role *name* from `GET /organizations/{id}/roles`, chosen from a dropdown. */
export interface AccessRuleRequest {
  ruleType: AccessRuleType;
  value: string;
  grantsRole: string;
  isActive?: boolean;
}

/** Mirrors `app.core.tenancy.schemas.AccessRule` -- the response has
 * `grantsRoleId` (a UUID), not the role name back; there is no backend
 * endpoint to resolve it to a display name, so the UI shows the id. */
export interface AccessRule {
  id: UUID;
  organizationId: UUID;
  ruleType: AccessRuleType;
  value: string;
  grantsRoleId: UUID;
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
}

export type SsoProtocol = "oidc" | "saml";

/** Mirrors `app.core.tenancy.schemas.SSOProvider` exactly -- the real
 * backend rejects anything else with a 422. */
export type SsoProviderKind = "entra_id" | "okta" | "auth0" | "google_workspace";

export interface SsoConfig {
  provider: SsoProviderKind;
  protocol: SsoProtocol;
  issuerUrl: string;
  clientId: string;
  clientSecretReference: string;
  enabled: boolean;
}
