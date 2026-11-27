import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { IngestionJobStats, IngestionQueueHealth, TenantStorage } from "@/types/ingestion";
import { mockIngestionQueueHealth, mockIngestionStats } from "@/mocks/data/ingestion";

/** `GET /observability/ingestion` -- per-connector run aggregates for the caller's organization. */
export async function listIngestionStats(since?: string): Promise<IngestionJobStats[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockIngestionStats);
  }
  const query = since ? `?since=${encodeURIComponent(since)}` : "";
  return apiRequest<IngestionJobStats[]>(`/observability/ingestion${query}`);
}

/** `GET /observability/ingestion/queue` -- live queue pressure (503 when the job queue is unreachable). */
export async function getIngestionQueueHealth(): Promise<IngestionQueueHealth> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockIngestionQueueHealth);
  }
  return apiRequest<IngestionQueueHealth>(`/observability/ingestion/queue`);
}

/** `GET /observability/storage` -- the caller's own storage estimate from the latest daily snapshot. */
export async function getTenantStorage(): Promise<TenantStorage> {
  if (USE_MOCK_DATA) {
    return mockDelay({
      available: true,
      takenAt: new Date().toISOString(),
      chunkRows: 48210,
      documentRows: 3120,
      estimatedBytes: 182_000_000,
      isEstimate: true,
      basis: "row_share_of_chunk_and_document_tables",
      history: [],
    });
  }
  return apiRequest<TenantStorage>(`/observability/storage`);
}
