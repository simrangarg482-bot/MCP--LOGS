export interface AuthUser {
  id: string;
  name: string;
  email: string;
  avatarUrl?: string;
  organizationId: string;
  role: "owner" | "admin" | "member" | "viewer";
  // Flattened permission codes from `GET /auth/me` (`core.users.schemas.
  // UserProfile.permissions`) -- the same set `Identity.permissions`
  // enforces server-side. The frontend uses this only to hide/disable
  // actions for UX; the backend remains authoritative and re-checks every
  // request regardless of what this array says.
  permissions: string[];
}

export interface SessionTokens {
  accessToken: string;
  refreshToken: string;
  tokenType: "bearer";
  expiresIn: number;
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
