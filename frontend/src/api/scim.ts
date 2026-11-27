import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  MemberStatus,
  ScimConfigUpdate,
  ScimGroup,
  ScimIdentity,
  ScimIdentityPage,
  ScimOverview,
  ScimPendingAction,
  ScimToken,
  ScimTokenCreated,
} from "@/types/scim";
import { mockScimGroups, mockScimIdentities, mockScimOverview, mockScimPending, mockScimTokens } from "@/mocks/data/scim";

const base = (org: string) => `/organizations/${org}/scim`;

/** `GET /organizations/{org}/scim` (`tenancy:manage`). */
export async function getScimOverview(org: string): Promise<ScimOverview> {
  if (USE_MOCK_DATA) return mockDelay(mockScimOverview);
  return apiRequest<ScimOverview>(base(org));
}

export async function updateScimConfig(org: string, update: ScimConfigUpdate): Promise<ScimOverview> {
  if (USE_MOCK_DATA) return mockDelay({ ...mockScimOverview, ...update });
  return apiRequest<ScimOverview>(base(org), { method: "PUT", body: update });
}

export async function listScimTokens(org: string): Promise<ScimToken[]> {
  if (USE_MOCK_DATA) return mockDelay(mockScimTokens);
  return apiRequest<ScimToken[]>(`${base(org)}/tokens`);
}

/** The ONLY place the raw token is ever returned. */
export async function createScimToken(org: string, name: string, ttlDays: number): Promise<ScimTokenCreated> {
  if (USE_MOCK_DATA) {
    return mockDelay({ token: { ...mockScimTokens[0], name }, rawToken: "scim_mock_token_shown_once" });
  }
  return apiRequest<ScimTokenCreated>(`${base(org)}/tokens`, { method: "POST", body: { name, ttlDays } });
}

export async function rotateScimToken(org: string, tokenId: string): Promise<ScimTokenCreated> {
  if (USE_MOCK_DATA) return mockDelay({ token: mockScimTokens[0], rawToken: "scim_mock_rotated_token" });
  return apiRequest<ScimTokenCreated>(`${base(org)}/tokens/${tokenId}/rotate`, { method: "POST", body: {} });
}

export async function revokeScimToken(org: string, tokenId: string): Promise<ScimToken> {
  if (USE_MOCK_DATA) return mockDelay({ ...mockScimTokens[0], status: "revoked" });
  return apiRequest<ScimToken>(`${base(org)}/tokens/${tokenId}/revoke`, { method: "POST" });
}

export async function listScimIdentities(org: string, status?: "pending" | "linked"): Promise<ScimIdentityPage> {
  if (USE_MOCK_DATA) {
    return mockDelay({ items: mockScimIdentities, total: mockScimIdentities.length, offset: 0, limit: 50 });
  }
  const query = status ? `?status=${status}` : "";
  return apiRequest<ScimIdentityPage>(`${base(org)}/identities${query}`);
}

export async function linkScimIdentity(org: string, identityId: string, userId: string): Promise<ScimIdentity> {
  if (USE_MOCK_DATA) return mockDelay({ ...mockScimIdentities[0], linkedUserId: userId, linkedVia: "admin" });
  return apiRequest<ScimIdentity>(`${base(org)}/identities/${identityId}/link`, { method: "POST", body: { userId } });
}

export async function listScimPending(org: string): Promise<ScimPendingAction[]> {
  if (USE_MOCK_DATA) return mockDelay(mockScimPending);
  return apiRequest<ScimPendingAction[]>(`${base(org)}/pending`);
}

export async function decideScimPending(
  org: string,
  pendingId: string,
  decision: "approve" | "reject",
): Promise<ScimPendingAction> {
  if (USE_MOCK_DATA) {
    return mockDelay({ ...mockScimPending[0], status: decision === "approve" ? "approved" : "rejected" });
  }
  return apiRequest<ScimPendingAction>(`${base(org)}/pending/${pendingId}/${decision}`, { method: "POST" });
}

// --- member access (Phase 1 endpoints) ---------------------------------------------------------------

export async function deactivateMember(org: string, userId: string, reason: string): Promise<MemberStatus> {
  if (USE_MOCK_DATA) {
    return mockDelay({
      userId,
      organizationId: org,
      status: "deactivated",
      source: "admin",
      reason,
      deactivatedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      memberEpoch: 1,
    });
  }
  return apiRequest<MemberStatus>(`/organizations/${org}/members/${userId}/deactivate`, {
    method: "POST",
    body: { reason },
  });
}

export async function reactivateMember(org: string, userId: string, reason: string): Promise<MemberStatus> {
  if (USE_MOCK_DATA) {
    return mockDelay({
      userId,
      organizationId: org,
      status: "active",
      source: "admin",
      reason,
      deactivatedAt: null,
      updatedAt: new Date().toISOString(),
      memberEpoch: 2,
    });
  }
  return apiRequest<MemberStatus>(`/organizations/${org}/members/${userId}/reactivate`, {
    method: "POST",
    body: { reason },
  });
}

// --- groups and role mapping (Phase 4) ---------------------------------------------------------------

/** `GET /organizations/{org}/scim/groups` (`tenancy:manage`). */
export async function listScimGroups(org: string): Promise<ScimGroup[]> {
  if (USE_MOCK_DATA) return mockDelay(mockScimGroups);
  return apiRequest<ScimGroup[]>(`${base(org)}/groups`);
}

/** `PUT /organizations/{org}/scim/groups/{id}/roles` -- makes `roleIds` exactly the roles the group grants. */
export async function setScimGroupRoles(org: string, groupId: string, roleIds: string[]): Promise<ScimGroup> {
  if (USE_MOCK_DATA) return mockDelay({ ...mockScimGroups[0], roles: [] });
  return apiRequest<ScimGroup>(`${base(org)}/groups/${groupId}/roles`, { method: "PUT", body: { roleIds } });
}
