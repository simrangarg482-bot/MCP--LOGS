import type { ScimGroup, ScimIdentity, ScimOverview, ScimPendingAction, ScimToken } from "@/types/scim";

export const mockScimOverview: ScimOverview = {
  platformEnabled: true,
  enabled: true,
  endpointUrl: "https://api.example.com/scim/v2",
  guardMinAbsolute: 5,
  guardPercent: 20,
  guardWindowMinutes: 60,
  reactivationSettleSeconds: 300,
  lastRequestAt: new Date().toISOString(),
  lastSuccessAt: new Date().toISOString(),
  lastErrorCode: null,
  lastErrorAt: null,
  activeTokens: 1,
  pendingDeactivations: 0,
  pendingIdentities: 0,
};

export const mockScimTokens: ScimToken[] = [
  {
    id: "tok-1",
    name: "Entra ID",
    prefix: "scim_AbCdEfGh",
    createdAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 90 * 86400000).toISOString(),
    lastUsedAt: new Date().toISOString(),
    revokedAt: null,
    status: "active",
  },
];

export const mockScimIdentities: ScimIdentity[] = [
  {
    id: "ident-1",
    userName: "ada@example.com",
    externalId: "00u1",
    displayName: "Ada",
    active: true,
    linkedUserId: null,
    linkedVia: null,
    linkedAt: null,
    lastSeenAt: new Date().toISOString(),
    candidates: [],
  },
];

export const mockScimPending: ScimPendingAction[] = [];

export const mockScimGroups: ScimGroup[] = [
  {
    id: "scim-group-1",
    displayName: "Engineering",
    externalId: null,
    memberCount: 12,
    roles: [{ id: "role-engineer", name: "engineer" }],
    updatedAt: new Date().toISOString(),
  },
];
