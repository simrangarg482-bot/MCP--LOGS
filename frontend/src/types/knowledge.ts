import type { ISODateString, UUID } from "./common";

/** Mirrors `app.core.tenancy.schemas.ConnectorSource` plus `"manual"` (human/agent-proposed documents). */
export type KnowledgeSource =
  | "github"
  | "slack"
  | "manual"
  | "teams"
  | "azure_devops"
  | "jira"
  | "confluence"
  | "sharepoint"
  | "runbooks"
  | "monitoring";

/** Mirrors `app.shared.schemas.DocumentStatus` -- a rejected proposal is soft-deleted, not a third status value. */
export type DocumentStatus = "published" | "proposed";

/** Mirrors `app.core.knowledge.schemas.Document`. */
export interface KnowledgeDocument {
  id: UUID;
  organizationId: UUID;
  projectId: UUID;
  title: string | null;
  status: DocumentStatus;
  version: number;
  content: string | null;
  source: KnowledgeSource;
  sourceUrl: string | null;
  sourceIncidentId: UUID | null;
  createdAt: ISODateString;
  updatedAt: ISODateString;
}

// Matches `app.shared.schemas.agent_contracts.GapReport` field-for-field --
// the previous shape (`topic`, `description`, `relatedIncidentIds`,
// `detectedAt`, `severity`) matched nothing the real `GET /knowledge/gaps`
// response actually returns.
export interface GapReport {
  id: UUID;
  organizationId: UUID;
  suggestedTopic: string;
  supportingExecutionIds: string[];
  suggestedAction: "new_runbook" | "update_existing";
  relatedDocumentId: UUID | null;
  status: "open" | "dismissed";
  createdAt: ISODateString;
  updatedAt: ISODateString;
}

export interface KnowledgeFilters {
  search?: string;
  source?: KnowledgeSource[];
  page?: number;
  pageSize?: number;
}

/** Mirrors `app.core.knowledge.schemas.DocumentUpdate` -- both fields
 * optional, `exclude_unset` on the backend (an omitted field is left
 * untouched, never cleared to null). */
export interface DocumentUpdateRequest {
  title?: string;
  content?: string;
}
