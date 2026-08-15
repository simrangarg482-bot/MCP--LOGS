/**
 * Access-token holder shared between AuthContext and the API client (kept
 * outside React so apiRequest() can read it without a hook dependency), plus
 * refresh-token persistence so a page reload doesn't force a fresh login.
 *
 * Only the refresh token is persisted to localStorage -- the short-lived
 * access token stays in memory and is re-minted from the refresh token on
 * load (via POST /auth/refresh), the same pattern the backend's SSO flow
 * already assumes.
 */
const REFRESH_TOKEN_KEY = "ekip.refresh_token";

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setRefreshToken(token: string | null): void {
  if (token) {
    localStorage.setItem(REFRESH_TOKEN_KEY, token);
  } else {
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }
}

export function clearSession(): void {
  accessToken = null;
  setRefreshToken(null);
}

/**
 * Fired when `apiRequest` (api/client.ts) receives a 401 -- the access
 * token is expired/invalid and there is no automatic silent-refresh-and-
 * retry path (yet), so the session is cleared and every listener (just
 * `AuthContext` today) reacts by clearing its own `user` state, which makes
 * `ProtectedRoute` redirect to `/login` through its existing, ordinary
 * "not authenticated" path -- no separate redirect mechanism needed.
 *
 * A plain DOM event, not a direct function call into `AuthContext`, because
 * `api/client.ts` is a plain module with no React context to call into;
 * this keeps `client.ts` and `AuthContext.tsx` decoupled through the one
 * module (`tokenStore.ts`) both already depend on, rather than introducing
 * a new import cycle between them.
 */
export const SESSION_EXPIRED_EVENT = "ekip:session-expired";

export function clearSessionAndNotifyExpired(): void {
  clearSession();
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

let pendingRefresh: Promise<unknown> | null = null;

/**
 * Coalesces concurrent refresh attempts into one in-flight request.
 *
 * Refresh tokens are single-use and rotated server-side, with reuse
 * detection that revokes the *entire* token family the moment an
 * already-rotated token is presented a second time (`core.auth.service.
 * refresh`'s own docstring). React 18 StrictMode deliberately double-invokes
 * effects in development -- without this guard, AuthProvider's mount effect
 * fires two concurrent `POST /auth/refresh` calls with the same stored
 * token: the first rotates it and succeeds, the second is then treated as a
 * replay of an already-rotated token and kills the session the first call
 * had just established. Confirmed via a live end-to-end run (a page reload
 * bounced straight back to /login) before this guard was added.
 */
export function dedupedRefresh<T>(
  refreshTokenValue: string,
  performRefresh: (token: string) => Promise<T>,
): Promise<T> {
  if (pendingRefresh) {
    return pendingRefresh as Promise<T>;
  }
  const promise = performRefresh(refreshTokenValue).finally(() => {
    pendingRefresh = null;
  });
  pendingRefresh = promise;
  return promise;
}

interface AccessTokenClaims {
  sub?: string;
  organization_id?: string;
  exp?: number;
}

/**
 * Decodes the (unverified) payload of a JWT access token for client-side
 * display purposes only -- e.g. reading `organization_id`, which `GET
 * /auth/me` doesn't return. The backend re-verifies the real signature on
 * every request; nothing here is a trust boundary.
 */
export function decodeAccessTokenClaims(token: string): AccessTokenClaims | null {
  try {
    const payload = token.split(".")[1];
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(normalized)) as AccessTokenClaims;
  } catch {
    return null;
  }
}
