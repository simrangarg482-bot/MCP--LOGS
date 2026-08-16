import type { Incident, TimelineEntry } from "@/types/incident";
import { hoursAgo, minutesAgo, daysAgo } from "@/mocks/time";

const ORG_ID = "org-1";
const PROJECT_ID = "project-1";
const reporters = ["user-1", "user-2", "user-3", "user-4"];

export const mockIncidents: Incident[] = [
  {
    id: "inc-1024",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Payment API returning 500 errors",
    description: "Elevated 5xx rate on the payment-service checkout endpoint following the 14:02 UTC deploy.",
    severity: "critical",
    status: "investigating",
    ownerTeam: "Payments",
    reportedBy: reporters[0],
    createdAt: minutesAgo(12),
    updatedAt: minutesAgo(3),
    resolvedAt: null,
  },
  {
    id: "inc-1023",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Elevated checkout latency on EU cluster",
    description: "P99 latency on checkout-service exceeded 4s in eu-west-1 after a connection pool exhaustion event.",
    severity: "high",
    status: "open",
    ownerTeam: "Payments",
    reportedBy: reporters[1],
    createdAt: hoursAgo(1),
    updatedAt: minutesAgo(20),
    resolvedAt: null,
  },
  {
    id: "inc-1022",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Auth token refresh failures for SSO users",
    description: "A subset of Entra ID users are receiving invalid_grant errors on silent token refresh.",
    severity: "medium",
    status: "resolved",
    ownerTeam: "Auth",
    reportedBy: reporters[2],
    createdAt: hoursAgo(3),
    updatedAt: hoursAgo(1),
    resolvedAt: hoursAgo(1),
  },
  {
    id: "inc-1021",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Knowledge ingestion worker backlog growing",
    description: "Confluence ingestion queue depth has grown steadily since the connector sync at 06:00 UTC.",
    severity: "low",
    status: "open",
    ownerTeam: "Ingestion",
    reportedBy: reporters[3],
    createdAt: hoursAgo(6),
    updatedAt: hoursAgo(2),
    resolvedAt: null,
  },
  {
    id: "inc-1020",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "GitHub connector rate limited",
    description: "Secondary rate limiting from the GitHub API caused a partial ingestion failure for two repositories.",
    severity: "medium",
    status: "resolved",
    ownerTeam: "Connectors",
    reportedBy: reporters[0],
    createdAt: daysAgo(1),
    updatedAt: hoursAgo(20),
    resolvedAt: hoursAgo(20),
  },
  {
    id: "inc-1019",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Database connection pool saturation",
    description: "postgres-primary reached 95% of max_connections during the nightly batch job.",
    severity: "high",
    status: "closed",
    ownerTeam: "Database",
    reportedBy: reporters[1],
    createdAt: daysAgo(2),
    updatedAt: daysAgo(1),
    resolvedAt: daysAgo(1),
  },
  {
    id: "inc-1018",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Slack connector webhook signature mismatch",
    description: "Incoming Slack events were rejected due to a rotated signing secret not yet propagated.",
    severity: "low",
    status: "closed",
    ownerTeam: "Connectors",
    reportedBy: reporters[2],
    createdAt: daysAgo(3),
    updatedAt: daysAgo(3),
    resolvedAt: daysAgo(3),
  },
  {
    id: "inc-1017",
    organizationId: ORG_ID,
    projectId: PROJECT_ID,
    title: "Cross-encoder reranker latency spike",
    description: "The retrieval reranking stage exceeded its 800ms budget for roughly 40 minutes.",
    severity: "medium",
    status: "resolved",
    ownerTeam: "Retrieval",
    reportedBy: reporters[3],
    createdAt: daysAgo(4),
    updatedAt: daysAgo(4),
    resolvedAt: daysAgo(4),
  },
];

// Real event types only ("note", "investigation") -- see types/incident.ts's
// TimelineEventType docstring for why the previous six-type mock roster
// (created/status_change/severity_change/assignment/agent_execution/
// connector_event/resolution) matched nothing the backend produces.
export const mockTimeline: Record<string, TimelineEntry[]> = {
  "inc-1024": [
    {
      id: "tl-3",
      organizationId: ORG_ID,
      incidentId: "inc-1024",
      eventType: "investigation",
      eventData: {
        evidence: [
          {
            source: "commit",
            reference: "payment-service@a1b2c3d",
            summary: "Deployed payment-service v2.14.0 at 14:02 UTC, introducing the promo-code refactor.",
            retrievedAt: minutesAgo(8),
            sourceTimestamp: minutesAgo(20),
            metadata: {},
          },
        ],
        hypotheses: [
          {
            description: "Null discount configuration object introduced by the promo-code refactor in v2.14.0.",
            confidence: 0.82,
            supportingEvidenceIds: ["payment-service@a1b2c3d"],
          },
        ],
        suggestedOwnerTeam: "Payments",
        suggestedNextSteps: [
          "Roll back payment-service to v2.13.4 to stop active customer impact.",
          "Backfill promo_rules for the 3 campaigns missing configuration.",
        ],
      },
      actor: "agent:investigation_agent",
      occurredAt: minutesAgo(8),
    },
    {
      id: "tl-6",
      organizationId: ORG_ID,
      incidentId: "inc-1024",
      eventType: "note",
      eventData: {
        note: "Confirmed the 14:02 UTC deploy of payment-service v2.14.0 correlates with the error spike. Rolling back.",
      },
      actor: "user:simran.kaur@example.com",
      occurredAt: minutesAgo(3),
    },
  ],
};
