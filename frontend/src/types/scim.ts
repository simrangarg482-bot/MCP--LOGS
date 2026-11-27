import type { ISODateString, UUID } from "./common";

// Matches `app.core.scim.admin.ScimOverview` (camelCased by the API client).
export interface ScimOverview {
  /** The deployment-wide kill switch (SCIM_ENABLED). When false nothing works, whatever the organization chose. */
  platformEnabled: boolean;
  enabled: boolean;
  endpointUrl: string;
  guardMinAbsolute: number;
  guardPercent: number;
  guardWindowMinutes: number;
  reactivationSettleSeconds: number;
  lastRequestAt: ISODateString | null;
  lastSuccessAt: ISODateString | null;
  lastErrorCode: string | null;
  lastErrorAt: ISODateString | null;
  activeTokens: number;
  pendingDeactivations: number;
  pendingIdentities: number;
}

export type ScimTokenStatus = "active" | "expired" | "revoked";

export interface ScimToken {
  id: UUID;
  name: string;
  prefix: string;
  createdAt: ISODateString;
  expiresAt: ISODateString;
  lastUsedAt: ISODateString | null;
  revokedAt: ISODateString | null;
  status: ScimTokenStatus;
}

export interface ScimTokenCreated {
  token: ScimToken;
  /** Shown ONCE. Only its hash is stored server-side; it cannot be retrieved again. */
  rawToken: string;
}

export interface ScimConfigUpdate {
  enabled?: boolean;
  guardMinAbsolute?: number;
  guardPercent?: number;
  guardWindowMinutes?: number;
  reactivationSettleSeconds?: number;
}

export interface CandidateMember {
  userId: UUID;
  email: string;
  displayName: string;
}

export interface ScimIdentity {
  id: UUID;
  userName: string;
  externalId: string | null;
  displayName: string | null;
  active: boolean;
  linkedUserId: UUID | null;
  linkedVia: "sso_subject" | "admin" | null;
  linkedAt: ISODateString | null;
  lastSeenAt: ISODateString;
  /** Members whose email equals the userName: a HINT for the administrator. It links nothing by itself. */
  candidates: CandidateMember[];
}

export interface ScimIdentityPage {
  items: ScimIdentity[];
  total: number;
  offset: number;
  limit: number;
}

export interface ScimPendingAction {
  id: UUID;
  userId: UUID;
  userName: string;
  reason: string;
  status: "pending" | "approved" | "rejected";
  createdAt: ISODateString;
  decidedAt: ISODateString | null;
}

// Matches `app.core.users.schemas.MemberStatus`.
export interface MemberStatus {
  userId: UUID;
  organizationId: UUID;
  status: "active" | "deactivated";
  source: "admin" | "scim" | null;
  reason: string | null;
  deactivatedAt: ISODateString | null;
  updatedAt: ISODateString | null;
  memberEpoch: number;
}

/** A group the identity provider pushed, with the roles an administrator mapped it to. It grants nothing until mapped. */
export interface ScimGroup {
  id: UUID;
  displayName: string;
  externalId: string | null;
  memberCount: number;
  roles: Array<{ id: UUID; name: string }>;
  updatedAt: ISODateString;
}
