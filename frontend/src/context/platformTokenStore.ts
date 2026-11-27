/**
 * The platform-operator access token lives in this module variable and NOWHERE else: not localStorage,
 * not sessionStorage, not a cookie, not the tenant `tokenStore`. A page reload therefore ends the
 * operator session (there is no platform refresh token either), which is the intended trade-off for a
 * 15-minute, high-privilege credential. Tenant tokens are never read here and platform tokens are never
 * read by the tenant API client, so neither can be presented in the other's place.
 */
let platformToken: string | null = null;

export function getPlatformToken(): string | null {
  return platformToken;
}

export function setPlatformToken(token: string | null): void {
  platformToken = token;
}

export function clearPlatformToken(): void {
  platformToken = null;
}

/** The token's own `exp` (seconds since epoch), for scheduling the sign-out. Display only, never a trust decision. */
export function platformTokenExpiry(token: string): number | null {
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const exp = (JSON.parse(atob(payload)) as { exp?: unknown }).exp;
    return typeof exp === "number" ? exp : null;
  } catch {
    return null;
  }
}
