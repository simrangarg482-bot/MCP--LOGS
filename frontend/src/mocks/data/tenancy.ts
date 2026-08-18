import type { AccessRule, Organization, OrgUser, Project, SsoConfig } from "@/types/tenancy";

export const mockOrganizations: Organization[] = [
  { id: "org-1", name: "Acme Corp", slug: "acme-corp" },
  { id: "org-2", name: "Northwind Traders", slug: "northwind" },
];

export const mockProjects: Project[] = [
  { id: "proj-1", organizationId: "org-1", name: "Engineering Platform", isDefault: true, createdAt: "2026-01-05T09:00:00Z" },
  { id: "proj-2", organizationId: "org-1", name: "Payments", isDefault: false, createdAt: "2026-02-12T14:30:00Z" },
  { id: "proj-3", organizationId: "org-2", name: "Core Services", isDefault: true, createdAt: "2026-01-20T11:15:00Z" },
];

export const mockUsers: OrgUser[] = [
  { id: "user-1", name: "Simran Kaur", email: "simran.kaur@acme.corp", role: "admin", status: "active", lastActiveAt: "2026-08-11T08:12:00Z" },
  { id: "user-2", name: "Rahul Mehta", email: "rahul.mehta@acme.corp", role: "member", status: "active", lastActiveAt: "2026-08-11T07:40:00Z" },
  { id: "user-3", name: "Priya Nair", email: "priya.nair@acme.corp", role: "member", status: "active", lastActiveAt: "2026-08-10T21:05:00Z" },
  { id: "user-4", name: "Daniel Osei", email: "daniel.osei@acme.corp", role: "viewer", status: "invited" },
  { id: "user-5", name: "Bhawna Relhan", email: "bhawna.relhan@navikenz.com", role: "owner", status: "active", lastActiveAt: "2026-08-11T09:02:00Z" },
];

export const mockAccessRules: AccessRule[] = [
  {
    id: "access-rule-1",
    organizationId: "org-1",
    ruleType: "domain",
    value: "acme.corp",
    grantsRoleId: "role-member",
    isActive: true,
    createdAt: "2026-06-01T10:00:00Z",
    updatedAt: "2026-06-01T10:00:00Z",
  },
  {
    id: "access-rule-2",
    organizationId: "org-1",
    ruleType: "group",
    value: "engineering",
    grantsRoleId: "role-engineer",
    isActive: false,
    createdAt: "2026-05-15T09:30:00Z",
    updatedAt: "2026-07-01T14:00:00Z",
  },
];

export const mockSsoConfig: SsoConfig = {
  provider: "entra_id",
  protocol: "oidc",
  issuerUrl: "https://login.microsoftonline.com/acme-corp/v2.0",
  clientId: "8f14e45f-ceea-4b3f-8d7c-000000000000",
  clientSecretReference: "vault://ekip/sso/entra-id/client-secret",
  enabled: true,
};
