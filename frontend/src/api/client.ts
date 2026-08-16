import type { ApiError } from "@/types/common";
import { API_BASE_URL } from "./config";
import { clearSessionAndNotifyExpired, getAccessToken } from "@/context/tokenStore";
import { keysToCamelCase, keysToSnakeCase } from "./caseConversion";

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Thin fetch wrapper for the EKIP FastAPI backend. All real (non-mock)
 * resource modules in src/api/* route through this function so that auth
 * headers, base URL resolution, and error shaping stay in one place.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const token = getAccessToken();

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(keysToSnakeCase(options.body)) : undefined,
    signal: options.signal,
  });

  if (!response.ok) {
    const error: ApiError = {
      status: response.status,
      message: response.statusText || "Request failed",
    };
    try {
      const payload = await response.json();
      // Every real `EKIPError` body is `{error_code, message, detail}`
      // (app/api/errors.py) -- previously only a generic `response.
      // statusText` ("Conflict", "Not Found") reached the UI, and
      // `error_code` was dropped entirely, making it impossible for a
      // caller to distinguish two different 409s (e.g. "postmortem already
      // exists" vs "incident not resolved yet") from each other.
      if (typeof payload?.message === "string") {
        error.message = payload.message;
      }
      error.errorCode = payload?.error_code;
      error.detail = payload?.detail;
    } catch {
      // response had no JSON body
    }

    // A 401 on a request that carried a Bearer token means the session
    // itself died (expired/revoked access token) -- previously this just
    // surfaced as a generic per-widget error, with no way back to a working
    // state short of a manual page reload. A 401 with NO token attached
    // (e.g. a wrong-password `/auth/login` attempt) is a different, local
    // failure -- there is no session to expire yet -- so this must not fire
    // for those or every failed login attempt would look like a forced
    // logout. See tokenStore.clearSessionAndNotifyExpired for the reactive
    // side (AuthContext clears its user state, ProtectedRoute redirects).
    if (response.status === 401 && token) {
      clearSessionAndNotifyExpired();
    }

    throw error;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return keysToCamelCase<T>(await response.json());
}

/** Simulates network latency for the mock data layer so loading states are visible. */
export function mockDelay<T>(value: T, ms = 350): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}
