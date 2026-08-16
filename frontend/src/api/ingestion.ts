import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { IngestionRun } from "@/types/ingestion";
import { mockIngestionRuns } from "@/mocks/data/ingestion";

/**
 * `GET /tenancy/connectors/{connectorConfigId}/runs`
 * (`core.tenancy.service.list_ingestion_runs`, gated by the existing
 * `tenancy:manage` permission via `get_connector` -- no new permission was
 * introduced for this, a Phase 2D addition).
 */
export async function listIngestionRuns(connectorConfigId: string): Promise<IngestionRun[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockIngestionRuns[connectorConfigId] ?? []);
  }
  return apiRequest<IngestionRun[]>(`/tenancy/connectors/${connectorConfigId}/runs`);
}
