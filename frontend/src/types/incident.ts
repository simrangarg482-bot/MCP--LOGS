import type { ISODateString, UUID } from "./common";
import type { EvidenceItem, RootCauseHypothesis } from "./ask";

export type IncidentSeverity = "critical" | "high" | "medium" | "low";

// Matches `app.shared.schemas.common.IncidentStatus` exactly -- the backend
// has no "monitoring" state (a value the frontend previously invented).
export type IncidentStatus = "open" | "investigating" | "resolved" | "closed";

// Matches `app.core.incidents.schemas.Incident` field-for-field. The
// previous shape (`displayId`, `service`, `assignee`, `tags`) matched
// nothing the real backend returns -- there is no assignee concept and no
// "service" field, only `ownerTeam` (nullable free text) and `projectId`.
export interface Incident {
  id: UUID;
  organizationId: UUID;
  projectId: UUID;
  title: string;
  description: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  ownerTeam: string | null;
  reportedBy: UUID;
  resolvedAt: ISODateString | null;
  createdAt: ISODateString;
  updatedAt: ISODateString;
}

export interface IncidentCreatePayload {
  title: string;
  description: string;
  severity: IncidentSeverity;
  projectId?: string;
}

// Matches `app.core.incidents.schemas.IncidentUpdate` exactly -- the only
// three fields `update_incident` accepts, not an arbitrary `Partial<Incident>`.
export interface IncidentUpdatePayload {
  status?: IncidentStatus;
  severity?: IncidentSeverity;
  ownerTeam?: string | null;
}

// Matches `app.core.incidents.schemas.TimelineEntry`/the two real
// `event_type` values `core.incidents.service` ever actually writes
// (`add_timeline_note` -> "note", `record_investigation_result` ->
// "investigation") -- the previous shape invented six event types
// (`created`, `status_change`, `severity_change`, `assignment`,
// `agent_execution`, `connector_event`, `resolution`) nothing on the
// backend ever produces, a `message: string` field that doesn't exist, and
// `createdAt` where the real column is `occurred_at`. There is also no
// separate "comments" concept on the backend at all -- a human note IS a
// timeline entry, not a different resource.
export type TimelineEventType = "note" | "investigation";

export interface TimelineNoteEventData {
  note: string;
}

// Mirrors `core.incidents.service.record_investigation_result`'s exact
// `event_data` shape (`evidence`/`hypotheses`/`suggested_owner_team`/
// `suggested_next_steps`, deep-camelCased at the API boundary) -- the same
// fields `InvestigationResult` (types/ask.ts) carries on `AskResponse`,
// since this is that same result, persisted.
export interface TimelineInvestigationEventData {
  evidence: EvidenceItem[];
  hypotheses: RootCauseHypothesis[];
  suggestedOwnerTeam: string | null;
  suggestedNextSteps: string[];
}

export interface TimelineEntry {
  id: UUID;
  organizationId: UUID;
  incidentId: UUID;
  eventType: TimelineEventType;
  eventData: TimelineNoteEventData | TimelineInvestigationEventData;
  actor: string;
  occurredAt: ISODateString;
}

// Request body for `POST /incidents/{id}/timeline`
// (`app.core.incidents.schemas.TimelineNoteCreate`) -- the field is `note`,
// not `body`; sending `{ body }` (the previous shape) would 422 for real.
export interface TimelineNoteCreatePayload {
  note: string;
}

// Matches `app.core.incidents.schemas.IncidentFilter` exactly: single-value
// severity/status/owner_team (never arrays), offset/limit pagination (the
// backend exposes no total-count query -- see `app/api/routers/incidents.py`
// `list_incidents`'s own docstring -- so there is no page-number pagination
// to build, and no free-text `search` filter exists at all).
export interface IncidentFilters {
  severity?: IncidentSeverity;
  status?: IncidentStatus;
  ownerTeam?: string;
  limit?: number;
  offset?: number;
}
