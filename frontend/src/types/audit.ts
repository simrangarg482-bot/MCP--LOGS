import type { ISODateString, UUID } from "./common";

// Matches `app.core.audit.schemas.AuditLogEntry` field-for-field.
export interface AuditLogEntry {
  id: UUID;
  organizationId: UUID | null;
  actor: string;
  action: string;
  resourceType: string;
  resourceId: UUID | null;
  eventMetadata: Record<string, unknown> | null;
  occurredAt: ISODateString;
}

// Matches `app.core.audit.schemas.AuditLogQuery` -- all optional, AND-combined.
export interface AuditLogFilters {
  resourceType?: string;
  resourceId?: string;
  actor?: string;
  limit?: number;
  offset?: number;
}
