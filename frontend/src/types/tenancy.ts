import type { UUID } from "./common";

export interface Organization {
  id: UUID;
  name: string;
  slug: string;
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
  status: "active" | "invited" | "suspended";
  lastActiveAt?: string;
}

/** `grantsRole` is a platform role *name* (e.g. "admin"), not one of this
 * file's `UserRole` literals -- same constraint as `AccessRuleRequest.
 * grantsRole` below: roles are a seeded catalog with no "list roles"
 * endpoint, so this is free text, not a select. In practice "admin" is the
 * only role any deployment of this codebase actually creates today
 * (`core.users.service.ensure_admin_role`) -- inviting as anything else
 * 422s with `role.not_found` until a real role-management surface exists. */
export interface InvitationRequest {
  email: string;
  grantsRole: string;
}

export interface Invitation {
  id: UUID;
  organizationId: UUID;
  email: string;
  status: "pending" | "accepted" | "expired" | "revoked";
  expiresAt: string;
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
 * platform role *name* (e.g. "engineer"), not one of this file's
 * `UserRole` literals -- roles are a seeded, organization-independent
 * catalog with no fixed set the frontend can enumerate; there is no
 * "list roles" endpoint to populate a dropdown from, so this is a free-text
 * field, not a select. */
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
