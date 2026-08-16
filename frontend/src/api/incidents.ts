import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  Incident,
  IncidentCreatePayload,
  IncidentFilters,
  IncidentUpdatePayload,
  TimelineEntry,
  TimelineNoteCreatePayload,
} from "@/types/incident";
import { mockIncidents, mockTimeline } from "@/mocks/data/incidents";

function applyFilters(items: Incident[], filters: IncidentFilters): Incident[] {
  let result = [...items];

  if (filters.severity) {
    result = result.filter((i) => i.severity === filters.severity);
  }
  if (filters.status) {
    result = result.filter((i) => i.status === filters.status);
  }
  if (filters.ownerTeam) {
    result = result.filter((i) => i.ownerTeam === filters.ownerTeam);
  }
  result.sort((a, b) => (a.createdAt > b.createdAt ? -1 : 1));

  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 50;
  return result.slice(offset, offset + limit);
}

/**
 * Returns a bare array, matching the real `GET /incidents` response exactly
 * -- the backend has no total-count query (see `app/api/routers/incidents.py`
 * `list_incidents`'s own docstring), so there is no `{items, total}`
 * envelope to return and no page-number UI this can honestly support, only
 * offset/limit ("load more").
 */
export async function listIncidents(filters: IncidentFilters = {}): Promise<Incident[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(applyFilters(mockIncidents, filters));
  }

  const params = new URLSearchParams();
  if (filters.severity) params.set("severity", filters.severity);
  if (filters.status) params.set("status", filters.status);
  if (filters.ownerTeam) params.set("owner_team", filters.ownerTeam);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));

  return apiRequest<Incident[]>(`/incidents?${params.toString()}`);
}

export async function createIncident(payload: IncidentCreatePayload): Promise<Incident> {
  if (USE_MOCK_DATA) {
    const now = new Date().toISOString();
    const incident: Incident = {
      id: `incident-${Date.now()}`,
      organizationId: "org-1",
      projectId: payload.projectId ?? "project-default",
      title: payload.title,
      description: payload.description,
      severity: payload.severity,
      status: "open",
      ownerTeam: null,
      reportedBy: "user-5",
      resolvedAt: null,
      createdAt: now,
      updatedAt: now,
    };
    return mockDelay(incident, 300);
  }
  return apiRequest<Incident>("/incidents", { method: "POST", body: payload });
}

export async function getIncident(id: string): Promise<Incident> {
  if (USE_MOCK_DATA) {
    const incident = mockIncidents.find((i) => i.id === id);
    if (!incident) throw { status: 404, message: "Incident not found" };
    return mockDelay(incident);
  }
  return apiRequest<Incident>(`/incidents/${id}`);
}

export async function getIncidentTimeline(id: string): Promise<TimelineEntry[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockTimeline[id] ?? []);
  }
  return apiRequest<TimelineEntry[]>(`/incidents/${id}/timeline`);
}

/**
 * `POST /incidents/{id}/timeline` (`app.core.incidents.schemas.
 * TimelineNoteCreate` -- the field is `note`, matching the payload type).
 * There is no separate "add a comment" endpoint; a human note IS a
 * timeline entry (`event_type="note"`), which is why this returns a real
 * `TimelineEntry`, not a bespoke comment shape.
 */
export async function addIncidentNote(id: string, note: string): Promise<TimelineEntry> {
  if (USE_MOCK_DATA) {
    const entry: TimelineEntry = {
      id: `tl-${Date.now()}`,
      organizationId: "org-1",
      incidentId: id,
      eventType: "note",
      eventData: { note },
      actor: "user:you@example.com",
      occurredAt: new Date().toISOString(),
    };
    return mockDelay(entry, 200);
  }
  const payload: TimelineNoteCreatePayload = { note };
  return apiRequest<TimelineEntry>(`/incidents/${id}/timeline`, { method: "POST", body: payload });
}

export async function updateIncident(id: string, patch: IncidentUpdatePayload): Promise<Incident> {
  if (USE_MOCK_DATA) {
    const incident = mockIncidents.find((i) => i.id === id);
    if (!incident) throw { status: 404, message: "Incident not found" };
    return mockDelay({ ...incident, ...patch, updatedAt: new Date().toISOString() }, 200);
  }
  return apiRequest<Incident>(`/incidents/${id}`, { method: "PATCH", body: patch });
}
