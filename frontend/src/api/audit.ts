import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { AuditLogEntry, AuditLogFilters } from "@/types/audit";
import { mockAuditLog } from "@/mocks/data/audit";

/**
 * `GET /organizations/{id}/audit` (`core.audit.service.query_audit_log`,
 * permission `audit:read`) -- a Phase 2C addition. This function existed
 * with a real, complete implementation but had zero REST/MCP callers
 * anywhere in the codebase before this; there was nothing fictional to fix
 * here, only a real capability with no frontend yet.
 */
export async function listAuditLog(
  organizationId: string,
  filters: AuditLogFilters = {},
): Promise<AuditLogEntry[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockAuditLog, 300);
  }
  const params = new URLSearchParams();
  if (filters.resourceType) params.set("resource_type", filters.resourceType);
  if (filters.resourceId) params.set("resource_id", filters.resourceId);
  if (filters.actor) params.set("actor", filters.actor);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return apiRequest<AuditLogEntry[]>(`/organizations/${organizationId}/audit?${params.toString()}`);
}
