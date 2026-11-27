import type { UUID } from "./common";

export type OrganizationStatus = "onboarding" | "active" | "suspended";
export type OperatorRole = "admin" | "viewer";

export interface PlatformSession {
  accessToken: string;
  tokenType: "bearer";
  expiresIn: number;
  role: OperatorRole;
}

export interface PlatformOperator {
  userId: UUID;
  email: string;
  displayName: string | null;
  role: OperatorRole;
}

export interface PlatformOrganizationSummary {
  id: UUID;
  name: string;
  slug: string;
  status: OrganizationStatus;
  statusChangedAt: string | null;
  createdAt: string;
  memberCount: number;
}

export interface PlatformOrganizationDetail extends PlatformOrganizationSummary {
  statusMessagePublic: string | null;
  sessionEpoch: number;
  historyCount: number;
}

export interface PlatformOrganizationList {
  items: PlatformOrganizationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface PlatformStatusHistoryEntry {
  id: UUID;
  previousStatus: OrganizationStatus;
  newStatus: "active" | "suspended";
  reason: string;
  customerMessage: string | null;
  /** Private to operators; never shown to tenants. */
  internalNote: string | null;
  operatorUserId: UUID;
  operatorEmail: string | null;
  changedAt: string;
  requestId: string | null;
}

export interface PlatformStatusHistory {
  items: PlatformStatusHistoryEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface StatusChangeResult {
  organization: PlatformOrganizationDetail;
  previousStatus: OrganizationStatus;
  newStatus: "active" | "suspended";
  historyId: UUID;
  notificationQueued: boolean;
}

export type LifecycleAction = "activate" | "suspend" | "reactivate";

export interface SuspensionNotice {
  organizationName: string | null;
  customerMessage: string | null;
}
