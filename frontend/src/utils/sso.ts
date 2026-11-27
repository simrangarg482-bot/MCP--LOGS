import type { ApiError } from "@/types/common";

/** Organization slugs are lowercase words joined by single hyphens (same rule as the backend). */
const SLUG_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;

export function normalizeSlug(input: string): string {
  return input.trim().toLowerCase();
}

export function isValidSlug(slug: string): boolean {
  return slug.length >= 1 && slug.length <= 63 && SLUG_PATTERN.test(slug);
}

/** Exactly the address the backend accepts and the identity provider must have registered. */
export function ssoCallbackUrl(slug: string): string {
  return `${window.location.origin}/o/${slug}/callback`;
}

export function ssoLoginUrl(slug: string): string {
  return `${window.location.origin}/o/${slug}/login`;
}

// --- the login in flight ----------------------------------------------------------
//
// Only the *reference* to the server-side transaction is kept in the browser: the opaque
// handle, the `state` echoed back by the provider (for an early sanity check -- the server
// remains the authority) and the organization. No token, no PKCE verifier, no nonce.
// sessionStorage: per tab, gone when the tab closes, never sent anywhere.

const STORAGE_KEY = "ekip.sso.transaction";
/** Matches the server's transaction lifetime (10 min by default) with a little slack. */
const MAX_AGE_MS = 15 * 60 * 1000;

export interface PendingSsoLogin {
  slug: string;
  state: string;
  transactionId: string;
  startedAt: number;
}

export function savePendingSsoLogin(login: Omit<PendingSsoLogin, "startedAt">): boolean {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ ...login, startedAt: Date.now() }));
    return true;
  } catch {
    return false; // storage blocked: the caller must not send the person away
  }
}

export function readPendingSsoLogin(): PendingSsoLogin | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingSsoLogin>;
    if (
      typeof value.slug !== "string" ||
      typeof value.state !== "string" ||
      typeof value.transactionId !== "string" ||
      typeof value.startedAt !== "number" ||
      Date.now() - value.startedAt > MAX_AGE_MS
    ) {
      return null;
    }
    return value as PendingSsoLogin;
  } catch {
    return null;
  }
}

export function clearPendingSsoLogin(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* nothing to clear */
  }
}

/** Only ever navigate the browser to an http(s) URL -- never `javascript:` or `data:`. */
export function isSafeRedirectTarget(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

// --- presentation -------------------------------------------------------------------

export type SsoProvider = "entra_id" | "okta" | "auth0" | "google_workspace";

export interface ProviderBrand {
  /** Text on the button: "Continue with Microsoft". */
  buttonLabel: string;
  /** Product name for headings. */
  name: string;
}

const BRANDS: Record<SsoProvider, ProviderBrand> = {
  entra_id: { buttonLabel: "Continue with Microsoft", name: "Microsoft Entra ID" },
  google_workspace: { buttonLabel: "Continue with Google", name: "Google Workspace" },
  okta: { buttonLabel: "Continue with Okta", name: "Okta" },
  auth0: { buttonLabel: "Continue with Auth0", name: "Auth0" },
};

export function providerBrand(provider: SsoProvider): ProviderBrand {
  return BRANDS[provider];
}

/** Provider-reported `error` values on the callback URL. */
export function providerErrorMessage(code: string): { title: string; description: string; cancelled: boolean } {
  if (code === "access_denied") {
    return {
      title: "Sign-in cancelled",
      description: "You cancelled sign-in, or your organization's identity provider did not allow it.",
      cancelled: true,
    };
  }
  if (code === "login_required" || code === "interaction_required" || code === "consent_required") {
    return {
      title: "More action needed at your provider",
      description: "Your identity provider needs you to sign in again. Start over to continue.",
      cancelled: false,
    };
  }
  return {
    title: "Your identity provider reported a problem",
    description: "Sign-in could not be completed. Try again, or ask your administrator if it keeps happening.",
    cancelled: false,
  };
}

/** Sentence for an error from begin / callback calls. */
export function ssoErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError> | undefined;
  switch (apiError?.errorCode) {
    case "sso.unavailable":
      return "Single sign-on isn't available for this organization.";
    case "auth.sso_transaction_invalid":
      return "This sign-in attempt expired or was already used. Start again.";
    case "auth.sso_state_mismatch":
      return "This sign-in response didn't match the attempt that started it. Start again.";
    case "auth.sso_unavailable":
    case "auth.idp_unreachable":
      return "Your identity provider couldn't be reached. Try again in a moment.";
    case "auth.idp_exchange_failed":
      return "Your identity provider rejected this sign-in. Start again.";
    case "auth.idp_token_invalid":
    case "auth.idp_nonce_mismatch":
    case "auth.idp_key_not_found":
    case "auth.idp_response_invalid":
      return "Your identity provider's response couldn't be verified, so you were not signed in.";
    case "auth.idp_email_not_verified":
      return "Your identity provider hasn't verified your email address, so you can't sign in with it.";
    case "auth.not_provisioned":
      return "You haven't been given access to this organization. Contact your administrator.";
    case "user.inactive":
      return "This account is inactive. Contact your administrator.";
    case "auth.email_not_verified":
    case "auth.account_email_unverified":
      return "An account with this email exists but its address hasn't been verified. Verify it from the email we sent, then sign in again.";
    case "auth.redirect_uri_not_allowed":
      return "This address isn't allowed to complete sign-in for this deployment. Contact your administrator.";
    case "rate_limited.ip":
      return "Too many attempts. Please wait a minute and try again.";
    default:
      return apiError?.message || "Something went wrong. Please try again.";
  }
}
