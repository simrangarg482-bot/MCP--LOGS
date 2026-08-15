import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { Invitation, InvitationRequest, Organization, OrgUser, Project, SsoConfig } from "@/types/tenancy";
import { mockOrganizations, mockProjects, mockSsoConfig, mockUsers } from "@/mocks/data/tenancy";

export async function listOrganizations(): Promise<Organization[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockOrganizations);
  }
  // Real backend: GET /organizations (no /tenancy prefix on this router --
  // and it always returns just the caller's own organization as a single-
  // element list, never every organization in the system; see
  // app/api/routers/tenancy.py's admin_router docstring).
  return apiRequest<Organization[]>(`/organizations`);
}

export async function listProjects(organizationId: string): Promise<Project[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockProjects.filter((p) => p.organizationId === organizationId));
  }
  return apiRequest<Project[]>(`/organizations/${organizationId}/projects`);
}

export async function listOrgUsers(): Promise<OrgUser[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockUsers);
  }
  // No real "list users in my organization" endpoint exists yet on the
  // backend (only /users/{id}/logout-all). Left pointed at the closest
  // plausible path so this fails loudly (404) instead of silently, until
  // that endpoint exists.
  return apiRequest<OrgUser[]>(`/users`);
}

export async function getSsoConfig(_organizationId: string): Promise<SsoConfig> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockSsoConfig);
  }
  // No read-back endpoint exists yet on the backend -- only
  // POST /organizations/{id}/sso/configure (write-only). Left pointed at
  // the closest plausible path so this fails loudly (404) instead of
  // silently, until a GET equivalent exists.
  return apiRequest<SsoConfig>(`/organizations/${_organizationId}/sso`);
}

/**
 * `POST /organizations/{id}/invitations` -- unlike `listOrgUsers`/
 * `getSsoConfig` above, this real endpoint exists and is fully implemented
 * (`core.tenancy.service.create_invitation`) as of this fix; the frontend
 * simply never called it before. The invited user won't show up in the
 * Users table afterward, though -- that table's own `listOrgUsers` call
 * still points at the not-yet-built `GET /users` (see that function's own
 * comment above); this only fixes the "Invite user" button, not the list's
 * separate, pre-existing gap.
 */
export async function createInvitation(
  organizationId: string,
  data: InvitationRequest,
): Promise<Invitation> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        id: "invitation-mock",
        organizationId,
        email: data.email,
        status: "pending",
        expiresAt: new Date(Date.now() + 14 * 24 * 60 * 60 * 1000).toISOString(),
      },
      300,
    );
  }
  return apiRequest<Invitation>(`/organizations/${organizationId}/invitations`, {
    method: "POST",
    body: data,
  });
}

export async function updateSsoConfig(organizationId: string, config: SsoConfig): Promise<SsoConfig> {
  if (USE_MOCK_DATA) {
    return mockDelay(config, 300);
  }
  return apiRequest<SsoConfig>(`/organizations/${organizationId}/sso/configure`, {
    method: "POST",
    body: config,
  });
}
