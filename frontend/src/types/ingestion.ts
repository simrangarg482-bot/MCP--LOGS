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
