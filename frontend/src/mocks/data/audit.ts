import type { AuditLogEntry } from "@/types/audit";
import { minutesAgo, hoursAgo } from "@/mocks/time";

export const mockAuditLog: AuditLogEntry[] = [
  {
    id: "audit-1",
    organizationId: "org-1",
    actor: "user:bhawna.relhan@navikenz.com",
    action: "tenancy.connector_config.register",
    resourceType: "connector_config",
    resourceId: "connector-1",
    eventMetadata: { source: "github" },
    occurredAt: minutesAgo(15),
  },
  {
    id: "audit-2",
    organizationId: "org-1",
    actor: "user:bhawna.relhan@navikenz.com",
    action: "incident.timeline_note.add",
    resourceType: "incident_timeline",
    resourceId: "tl-6",
    eventMetadata: { incident_id: "inc-1024" },
    occurredAt: hoursAgo(1),
  },
  {
    id: "audit-3",
    organizationId: "org-1",
    actor: "user:bhawna.relhan@navikenz.com",
    action: "tenancy.invitation.create",
    resourceType: "invitation",
    resourceId: "invitation-1",
    eventMetadata: { email: "new.engineer@example.com" },
    occurredAt: hoursAgo(3),
  },
];
