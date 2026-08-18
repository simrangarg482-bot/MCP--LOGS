import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { AuthUser, InvitationAcceptPayload, LoginPayload, SessionTokens, SignupPayload } from "@/types/auth";
import * as authApi from "@/api/auth";
import {
  clearSession,
  decodeAccessTokenClaims,
  dedupedRefresh,
  getRefreshToken,
  SESSION_EXPIRED_EVENT,
  setAccessToken,
  setRefreshToken,
} from "./tokenStore";

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  signup: (payload: SignupPayload) => Promise<void>;
  login: (payload: LoginPayload) => Promise<void>;
  acceptInvitation: (invitationId: string, payload: InvitationAcceptPayload) => Promise<void>;
  logout: () => Promise<void>;
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
      setAccessToken(tokens.accessToken);
      setRefreshToken(tokens.refreshToken);
      await loadUserFromAccessToken(tokens.accessToken);
    },
    [loadUserFromAccessToken],
  );

  const handleSignup = useCallback(
    async (payload: SignupPayload) => {
      await applySession(await authApi.signup(payload));
    },
    [applySession],
  );

  const handleLogin = useCallback(
    async (payload: LoginPayload) => {
      await applySession(await authApi.login(payload));
    },
    [applySession],
  );

  const handleAcceptInvitation = useCallback(
    async (invitationId: string, payload: InvitationAcceptPayload) => {
      await applySession(await authApi.acceptInvitation(invitationId, payload));
    },
    [applySession],
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

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      isLoading,
      signup: handleSignup,
      login: handleLogin,
      acceptInvitation: handleAcceptInvitation,
      logout: handleLogout,
    }),
    [user, isLoading, handleSignup, handleLogin, handleAcceptInvitation, handleLogout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- context files exporting both the Provider and its hook is the standard pattern here
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
