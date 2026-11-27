import type { ISODateString, UUID } from "./common";

// Matches `app.core.tenancy.schemas.IngestionRunStatus`/`app.shared.
// schemas.common.IngestionJobStatus` exactly.
export type IngestionRunStatus = "queued" | "running" | "succeeded" | "failed" | "dead_lettered";

// Matches `app.core.tenancy.schemas.IngestionRun` field-for-field -- no
// "chunks created" or structured error-list field exists on the real
// backend, only `documentsProcessed` (a count) and `failedStage` (a single
// free-text stage name, populated only when status is "failed").
export interface IngestionRun {
  id: UUID;
  organizationId: UUID;
  connectorConfigId: UUID;
  status: IngestionRunStatus;
  failedStage: string | null;
  documentsProcessed: number;
  pagesFetched?: number;
  itemsDiscovered?: number;
  itemsSkipped?: number;
  chunksEmbedded?: number;
  retryCount?: number;
  lastErrorType?: string | null;
  startedAt: ISODateString | null;
  completedAt: ISODateString | null;
  createdAt: ISODateString;
}

// Matches `app.core.tenancy.schemas.IngestionJobStats` field-for-field -- one row per connector,
// `GET /observability/ingestion` (permission `observability:read`). The backend aggregates finished
// outcomes only: there are no per-status "queued"/"running" counts here, and the row carries the
// connector's id but not its name.
export interface IngestionJobStats {
  connectorConfigId: UUID;
  runCount: number;
  succeededCount: number;
  failedCount: number;
  deadLetteredCount: number;
  /** Mean seconds of SUCCESSFUL runs only; null until one succeeds. */
  avgDurationSeconds: number | null;
  totalDocumentsProcessed: number;
  totalPagesFetched: number;
  totalItemsDiscovered: number;
  totalItemsSkipped: number;
  totalChunksEmbedded: number;
  totalRetries: number;
}

// Matches `app.core.tenancy.schemas.IngestionQueueHealth` -- `GET /observability/ingestion/queue`,
// read live from the job queue (Redis). It is the queue of jobs WAITING to start, not a count of
// running jobs.
export interface IngestionQueueHealth {
  queuedJobs: number;
  oldestQueuedAgeSeconds: number | null;
  workerMaxConcurrency: number;
}

/** `GET /observability/storage`: this organization's storage footprint. Bytes are an ESTIMATE (PostgreSQL does not attribute table size to tenants); row counts are exact as of `takenAt`. */
export interface TenantStorage {
  available: boolean;
  takenAt: string | null;
  chunkRows: number;
  documentRows: number;
  estimatedBytes: number | null;
  isEstimate: boolean;
  basis: string;
  history: Array<{ takenAt: string; estimatedBytes: number | null }>;
}
