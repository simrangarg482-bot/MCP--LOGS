import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  AuthUser,
  ChangePasswordPayload,
  InvitationAcceptPayload,
  LoginPayload,
  OrganizationSelectionPayload,
  OrganizationSelectionRequired,
  SessionTokens,
  SignupPayload,
  SsoCallbackPayload,
  SwitchOrganizationPayload,
} from "@/types/auth";
import * as authApi from "@/api/auth";
import {
  clearSession,
  decodeAccessTokenClaims,
  dedupedRefresh,
  getAccessToken,
  getRefreshToken,
  SESSION_EXPIRED_EVENT,
  setAccessToken,
  setRefreshToken,
} from "./tokenStore";
import { clearSuspensionNotice } from "./suspensionNotice";

// login's return value: undefined for the unchanged single-organization
// case (a session was already applied, same as before this fix existed),
// or the OrganizationSelectionRequired payload when the caller must show
// an organization picker before a session exists. Callers that only ever
// dealt with single-organization users can keep ignoring the return value
// entirely -- it stays undefined for them, exactly as before.
type LoginOutcome = OrganizationSelectionRequired | undefined;

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  // Email verification: signup creates nothing and starts no session -- it
  // only asks the backend to email a confirmation link. Resolving means "the
  // request was accepted", never "the user is signed in".
  signup: (payload: SignupPayload) => Promise<void>;
  login: (payload: LoginPayload) => Promise<LoginOutcome>;
  // Completes a multi-organization login after the caller shows the user
  // OrganizationSelectionRequired.organizations and they pick one.
  selectOrganization: (payload: OrganizationSelectionPayload) => Promise<void>;
  // Changes the signed-in user's password. The server ends every other session and returns a
  // fresh one for this organization, which replaces the stored tokens.
  changePassword: (payload: ChangePasswordPayload) => Promise<void>;
  // Completes an SSO login (after the provider redirected back): the server verifies the
  // single-use transaction, then the session is applied exactly like a password login's.
  completeSsoLogin: (payload: SsoCallbackPayload, redirectUri: string) => Promise<void>;
  // Re-scopes an already-authenticated session to a different organization
  // the user also belongs to (POST /auth/switch-organization).
  switchOrganization: (payload: SwitchOrganizationPayload) => Promise<void>;
  acceptInvitation: (invitationId: string, payload: InvitationAcceptPayload) => Promise<void>;
  logout: () => Promise<void>;
  // Re-reads `GET /auth/me` so roles/permissions changed on the server (e.g. an
  // administrator editing a role) show up without signing in again.
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadUserFromAccessToken = useCallback(async (accessToken: string) => {
    const claims = decodeAccessTokenClaims(accessToken);
    const organizationId = claims?.organization_id ?? "";
    const currentUser = await authApi.getCurrentUser(organizationId);
    setUser(currentUser);
  }, []);

  useEffect(() => {
    const storedRefreshToken = getRefreshToken();
    if (!storedRefreshToken) {
      setIsLoading(false);
      return;
    }
    dedupedRefresh(storedRefreshToken, authApi.refreshSession)
      .then(async (tokens) => {
        setAccessToken(tokens.accessToken);
        setRefreshToken(tokens.refreshToken);
        await loadUserFromAccessToken(tokens.accessToken);
      })
      .catch(() => {
        clearSession();
        setUser(null);
      })
      .finally(() => setIsLoading(false));
  }, [loadUserFromAccessToken]);

  useEffect(() => {
    // Fired by api/client.ts on a 401 from an authenticated request --
    // tokenStore.clearSessionAndNotifyExpired already cleared the stored
    // tokens; clearing `user` here is what actually makes `isAuthenticated`
    // false, which is what ProtectedRoute's existing redirect already keys
    // off of. No new redirect logic needed -- this reuses the ordinary
    // "not authenticated" path a fresh unauthenticated visitor already hits.
    const handleSessionExpired = () => setUser(null);
    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
  }, []);

  const applySession = useCallback(
    async (tokens: SessionTokens) => {
      // A working session supersedes any earlier "your organization is suspended" explanation.
      clearSuspensionNotice();
      setAccessToken(tokens.accessToken);
      setRefreshToken(tokens.refreshToken);
      await loadUserFromAccessToken(tokens.accessToken);
    },
    [loadUserFromAccessToken],
  );

  const handleSignup = useCallback(async (payload: SignupPayload) => {
    await authApi.signup(payload);
  }, []);

  const handleLogin = useCallback(
    async (payload: LoginPayload): Promise<LoginOutcome> => {
      const result = await authApi.login(payload);
      if (result.status === "organization_selection_required") {
        // Do NOT touch stored tokens/user state here -- login succeeding
        // for a multi-organization user does not mean a session exists yet.
        // The caller must show `result.organizations` and call
        // `selectOrganization` with the user's choice.
        return result;
      }
      await applySession(result);
      return undefined;
    },
    [applySession],
  );

  const handleSelectOrganization = useCallback(
    async (payload: OrganizationSelectionPayload) => {
      await applySession(await authApi.selectOrganization(payload));
    },
    [applySession],
  );

  const handleCompleteSsoLogin = useCallback(
    async (payload: SsoCallbackPayload, redirectUri: string) => {
      await applySession(await authApi.completeSsoLogin(payload, redirectUri));
    },
    [applySession],
  );

  const handleChangePassword = useCallback(
    async (payload: ChangePasswordPayload) => {
      await applySession(await authApi.changePassword(payload));
    },
    [applySession],
  );

  const handleSwitchOrganization = useCallback(
    async (payload: SwitchOrganizationPayload) => {
      // A fresh access + refresh token pair for the target organization --
      // applySession replaces the stored session outright (never merges),
      // matching switch_organization's "new token, not a mutated old one"
      // model server-side.
      await applySession(await authApi.switchOrganization(payload));
    },
    [applySession],
  );

  const handleAcceptInvitation = useCallback(
    async (invitationId: string, payload: InvitationAcceptPayload) => {
      // No session: acceptance completes when the emailed link is opened.
      await authApi.acceptInvitation(invitationId, payload);
    },
    [],
  );

  const handleLogout = useCallback(async () => {
    const storedRefreshToken = getRefreshToken();
    try {
      if (storedRefreshToken) {
        await authApi.logout(storedRefreshToken);
      }
    } finally {
      clearSession();
      setUser(null);
    }
  }, []);

  const handleRefreshUser = useCallback(async () => {
    const accessToken = getAccessToken();
    if (accessToken) await loadUserFromAccessToken(accessToken);
  }, [loadUserFromAccessToken]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      isLoading,
      signup: handleSignup,
      login: handleLogin,
      selectOrganization: handleSelectOrganization,
      completeSsoLogin: handleCompleteSsoLogin,
      changePassword: handleChangePassword,
      switchOrganization: handleSwitchOrganization,
      acceptInvitation: handleAcceptInvitation,
      logout: handleLogout,
      refreshUser: handleRefreshUser,
    }),
    [
      user,
      isLoading,
      handleSignup,
      handleLogin,
      handleSelectOrganization,
      handleCompleteSsoLogin,
      handleChangePassword,
      handleSwitchOrganization,
      handleAcceptInvitation,
      handleLogout,
      handleRefreshUser,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- context files exporting both the Provider and its hook is the standard pattern here
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
