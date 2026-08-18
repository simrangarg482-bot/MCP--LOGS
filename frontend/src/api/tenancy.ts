import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  AccessRule,
  AccessRuleRequest,
  Invitation,
  InvitationRequest,
  Organization,
  OrgUser,
  Project,
  SsoConfig,
  SsoProviderKind,
  SsoProtocol,
} from "@/types/tenancy";
import { mockAccessRules, mockOrganizations, mockProjects, mockSsoConfig, mockUsers } from "@/mocks/data/tenancy";

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

interface OrganizationMemberResponse {
  id: string;
  email: string;
  displayName: string;
  isActive: boolean;
  roles: string[];
  createdAt: string;
}

/**
 * `GET /organizations/{id}/members` -- a real Phase 2 addition
 * (`core.users.service.list_organization_members`); this previously pointed
 * at a `GET /users` endpoint that never existed on the backend at all.
 */
export async function listOrgUsers(organizationId: string): Promise<OrgUser[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockUsers);
  }
  const members = await apiRequest<OrganizationMemberResponse[]>(
    `/organizations/${organizationId}/members`,
  );
  return members.map((member) => ({
    id: member.id,
    name: member.displayName,
    email: member.email,
    // The backend returns every role name a member holds (alphabetically
    // sorted); the frontend's OrgUser model only has room for one, so this
    // picks the first -- the same simplification `getCurrentUser` already
    // makes for the current user (`roles[0]`), not a "most privileged role"
    // selection.
    role: (member.roles[0] as OrgUser["role"]) ?? "member",
    status: member.isActive ? "active" : "suspended",
  }));
}

// The real backend column/field is `client_secret_ref` (`app/core/tenancy/
// schemas.py`'s `SSOConfiguration`) -- one word short of this frontend
// type's `clientSecretReference`, so the generic snake<->camel converter in
// `apiRequest` can't bridge it (it becomes `clientSecretRef`, silently
// leaving `clientSecretReference` undefined). Both directions are
// translated explicitly here, at the one place that actually knows both
// names, rather than renaming either side's established naming.
interface SsoConfigWire {
  provider: SsoProviderKind;
  protocol: SsoProtocol;
  issuerUrl: string;
  clientId: string;
  clientSecretRef: string;
  enabled?: boolean;
}

export async function getSsoConfig(organizationId: string): Promise<SsoConfig> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockSsoConfig);
  }
  const wire = await apiRequest<SsoConfigWire>(`/organizations/${organizationId}/sso`);
  return {
    provider: wire.provider,
    protocol: wire.protocol,
    issuerUrl: wire.issuerUrl,
    clientId: wire.clientId,
    clientSecretReference: wire.clientSecretRef,
    enabled: wire.enabled ?? true,
  };
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
        token: "mock-invitation-token",
      },
      300,
    );
  }
  return apiRequest<Invitation>(`/organizations/${organizationId}/invitations`, {
    method: "POST",
    body: data,
  });
}

/** `GET /organizations/{id}/invitations` (`tenancy:manage`,
 * `core.tenancy.service.list_invitations`) -- every invitation regardless
 * of status; there is no server-side status filter. `token` is always
 * `undefined` here (see `Invitation.token`'s own docstring) -- only
 * `createInvitation`'s own response ever carries the raw value. */
export async function listInvitations(organizationId: string): Promise<Invitation[]> {
  if (USE_MOCK_DATA) {
    return mockDelay([]);
  }
  return apiRequest<Invitation[]>(`/organizations/${organizationId}/invitations`);
}

/** `POST /invitations/{id}/revoke` (`tenancy:manage`,
 * `core.tenancy.service.revoke_invitation`) -- no `organizationId` path
 * parameter; scoped to the caller's own org server-side. */
export async function revokeInvitation(invitationId: string): Promise<Invitation> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        id: invitationId,
        organizationId: "org-1",
        email: "revoked@example.com",
        status: "revoked",
        expiresAt: new Date().toISOString(),
      },
      300,
    );
  }
  return apiRequest<Invitation>(`/invitations/${invitationId}/revoke`, { method: "POST" });
}

/**
 * `GET /organizations/{id}/access-rules` (`tenancy:manage`,
 * `core.tenancy.service.list_access_rules`) -- returns every rule, active
 * or not; there is no server-side active-only filter.
 */
export async function listAccessRules(organizationId: string): Promise<AccessRule[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockAccessRules);
  }
  return apiRequest<AccessRule[]>(`/organizations/${organizationId}/access-rules`);
}

/**
 * `POST /organizations/{id}/access-rules` (`tenancy:manage`,
 * `core.tenancy.service.create_access_rule`). The backend does not reject
 * a duplicate `(ruleType, value)` pair -- neither does this function --
 * matching the real service's own lack of a uniqueness check.
 */
export async function createAccessRule(
  organizationId: string,
  data: AccessRuleRequest,
): Promise<AccessRule> {
  if (USE_MOCK_DATA) {
    const created: AccessRule = {
      id: `access-rule-${Date.now()}`,
      organizationId,
      ruleType: data.ruleType,
      value: data.value,
      grantsRoleId: `role-${data.grantsRole}`,
      isActive: data.isActive ?? true,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    mockAccessRules.unshift(created);
    return mockDelay(created, 300);
  }
  return apiRequest<AccessRule>(`/organizations/${organizationId}/access-rules`, {
    method: "POST",
    body: data,
  });
}

/**
 * `PATCH /access-rules/{ruleId}/deactivate` -- deliberately takes no
 * `organizationId`: the real endpoint always operates on the caller's own
 * organization (resolved server-side from the access token), never a URL
 * parameter (`app.api.routers.tenancy`'s own route has no
 * `{organization_id}` segment here). There is no reactivate endpoint --
 * this is a one-way action; the UI must not offer to undo it.
 */
export async function deactivateAccessRule(ruleId: string): Promise<AccessRule> {
  if (USE_MOCK_DATA) {
    const rule = mockAccessRules.find((r) => r.id === ruleId);
    if (!rule) throw { status: 404, message: "Access rule not found" };
    rule.isActive = false;
    rule.updatedAt = new Date().toISOString();
    return mockDelay(rule, 300);
  }
  return apiRequest<AccessRule>(`/access-rules/${ruleId}/deactivate`, { method: "PATCH" });
}

export async function updateSsoConfig(organizationId: string, config: SsoConfig): Promise<SsoConfig> {
  if (USE_MOCK_DATA) {
    return mockDelay(config, 300);
  }
  // `SSOConfigurationCreate` (app/core/tenancy/schemas.py) takes
  // `client_secret_ref`, not `client_secret_reference` -- see the matching
  // comment on `getSsoConfig`/`SsoConfigWire` above -- and has no `enabled`
  // field at all (the backend has no separate enable/disable state; a row
  // existing in `sso_configurations` *is* "enabled").
  const wire = await apiRequest<SsoConfigWire>(`/organizations/${organizationId}/sso/configure`, {
    method: "POST",
    body: {
      provider: config.provider,
      protocol: config.protocol,
      issuerUrl: config.issuerUrl,
      clientId: config.clientId,
      clientSecretRef: config.clientSecretReference,
    },
  });
  return {
    provider: wire.provider,
    protocol: wire.protocol,
    issuerUrl: wire.issuerUrl,
    clientId: wire.clientId,
    clientSecretReference: wire.clientSecretRef,
    enabled: true,
  };
}
