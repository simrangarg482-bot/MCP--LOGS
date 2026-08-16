import type { IngestionRun } from "@/types/ingestion";
import { minutesAgo, hoursAgo } from "@/mocks/time";

const MOCK_ORG_ID = "org-1";

export const mockIngestionRuns: Record<string, IngestionRun[]> = {
  "conn-github": [
    {
      id: "run-github-3",
      organizationId: MOCK_ORG_ID,
      connectorConfigId: "conn-github",
      status: "succeeded",
      failedStage: null,
      documentsProcessed: 128,
      startedAt: minutesAgo(14),
      completedAt: minutesAgo(12),
      createdAt: minutesAgo(14),
    },
    {
      id: "run-github-2",
      organizationId: MOCK_ORG_ID,
      connectorConfigId: "conn-github",
      status: "failed",
      failedStage: "embedding",
      documentsProcessed: 42,
      startedAt: hoursAgo(6),
      completedAt: hoursAgo(6),
      createdAt: hoursAgo(6),
    },
    {
      id: "run-github-1",
      organizationId: MOCK_ORG_ID,
      connectorConfigId: "conn-github",
      status: "succeeded",
      failedStage: null,
      documentsProcessed: 356,
      startedAt: hoursAgo(48),
      completedAt: hoursAgo(48),
      createdAt: hoursAgo(48),
    },
  ],
  "conn-slack": [
    {
      id: "run-slack-1",
      organizationId: MOCK_ORG_ID,
      connectorConfigId: "conn-slack",
      status: "succeeded",
      failedStage: null,
      documentsProcessed: 87,
      startedAt: minutesAgo(5),
      completedAt: minutesAgo(4),
      createdAt: minutesAgo(5),
    },
  ],
  "conn-confluence": [
    {
      id: "run-confluence-1",
      organizationId: MOCK_ORG_ID,
      connectorConfigId: "conn-confluence",
      status: "running",
      failedStage: null,
      documentsProcessed: 0,
      startedAt: minutesAgo(1),
      completedAt: null,
      createdAt: minutesAgo(1),
    },
  ],
};
