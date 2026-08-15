import type { ISODateString, UUID } from "./common";

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

export type TimelineEventType =
  | "created"
  | "status_change"
  | "severity_change"
  | "assignment"
  | "comment"
  | "agent_execution"
  | "connector_event"
  | "resolution";

export interface TimelineEntry {
  id: UUID;
  incidentId: UUID;
  type: TimelineEventType;
  actor: string;
  message: string;
  createdAt: ISODateString;
  metadata?: Record<string, string>;
}

export interface CitationSource {
  label: string;
  system: "github" | "slack" | "confluence" | "jira" | "incident" | "postgresql" | "other";
  reference: string;
  url?: string;
  timestamp?: ISODateString;
}

export interface RootCauseHypothesis {
  summary: string;
  confidence: number;
  evidence: string[];
}

export interface AiInvestigation {
  incidentId: UUID;
  summary: string;
  rootCauseHypotheses: RootCauseHypothesis[];
  relevantKnowledge: CitationSource[];
  similarIncidents: Array<{
    incident: Incident;
    similarityScore: number;
    matchedOn: string;
  }>;
  recommendedActions: string[];
  confidence: number;
  generatedAt: ISODateString;
  model: string;
}

export interface IncidentComment {
  id: UUID;
  incidentId: UUID;
  author: string;
  body: string;
  createdAt: ISODateString;
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
