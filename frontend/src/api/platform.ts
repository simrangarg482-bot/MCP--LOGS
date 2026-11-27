import type { ApiError } from "@/types/common";
import type {
  LifecycleAction,
  OrganizationStatus,
  PlatformOperator,
  PlatformOrganizationDetail,
  PlatformOrganizationList,
  PlatformSession,
  PlatformStatusHistory,
  StatusChangeResult,
} from "@/types/platform";
import { API_BASE_URL } from "./config";
import { keysToCamelCase, keysToSnakeCase } from "./caseConversion";
import { getPlatformToken } from "@/context/platformTokenStore";
import { isPlatformSessionFailure } from "@/utils/platform";

/** Dispatched when the server says the platform session or the whole surface is gone. */
export const PLATFORM_SESSION_FAILED_EVENT = "ekip:platform-session-failed";

/**
 * Fetch wrapper for `/platform/*`. Deliberately separate from `apiRequest`: it sends ONLY the platform
 * token (held in memory), never the tenant token, and does none of the tenant session handling (no
 * refresh, no tenant "session expired" event).
 */
async function platformRequest<T>(
  path: string,
  options: { method?: "GET" | "POST"; body?: unknown; auth?: boolean } = {},
): Promise<T> {
  const token = options.auth === false ? null : getPlatformToken();
  const response = await fetch(`${API_BASE_URL}/platform${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(keysToSnakeCase(options.body)) : undefined,
  });

  if (!response.ok) {
    const error: ApiError = { status: response.status, message: response.statusText || "Request failed" };
    try {
      const payload = await response.json();
      if (typeof payload?.message === "string") error.message = payload.message;
      error.errorCode = payload?.error_code;
      error.detail = payload?.detail;
    } catch {
      // no JSON body
    }
    if (token && isPlatformSessionFailure(error.errorCode)) {
      window.dispatchEvent(new CustomEvent(PLATFORM_SESSION_FAILED_EVENT, { detail: error.errorCode }));
    }
    throw error;
  }
  return keysToCamelCase<T>(await response.json());
}

export async function getPlatformAvailability(): Promise<boolean> {
  const result = await platformRequest<{ available: boolean }>("/status", { auth: false });
  return result.available;
}

export function platformLogin(email: string, password: string): Promise<PlatformSession> {
  return platformRequest<PlatformSession>("/auth/login", { method: "POST", body: { email, password }, auth: false });
}

export function getPlatformOperator(): Promise<PlatformOperator> {
  return platformRequest<PlatformOperator>("/auth/me");
}

export interface OrganizationQuery {
  search?: string;
  status?: OrganizationStatus | "";
  limit?: number;
  offset?: number;
}

export function listPlatformOrganizations(query: OrganizationQuery): Promise<PlatformOrganizationList> {
  const params = new URLSearchParams();
  if (query.search?.trim()) params.set("search", query.search.trim());
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? 25));
  params.set("offset", String(query.offset ?? 0));
  return platformRequest<PlatformOrganizationList>(`/organizations?${params.toString()}`);
}

export function getPlatformOrganization(id: string): Promise<PlatformOrganizationDetail> {
  return platformRequest<PlatformOrganizationDetail>(`/organizations/${encodeURIComponent(id)}`);
}

export function getPlatformStatusHistory(id: string, offset = 0): Promise<PlatformStatusHistory> {
  return platformRequest<PlatformStatusHistory>(
    `/organizations/${encodeURIComponent(id)}/status-history?limit=50&offset=${offset}`,
  );
}

export interface TransitionInput {
  reason: string;
  customerMessage?: string;
  internalNote?: string;
}

export function transitionOrganization(
  id: string,
  action: LifecycleAction,
  input: TransitionInput,
): Promise<StatusChangeResult> {
  const body: Record<string, string> = { reason: input.reason.trim() };
  if (action === "suspend") {
    if (input.customerMessage?.trim()) body.customerMessage = input.customerMessage.trim();
    if (input.internalNote?.trim()) body.internalNote = input.internalNote.trim();
  }
  return platformRequest<StatusChangeResult>(`/organizations/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    body,
  });
}
