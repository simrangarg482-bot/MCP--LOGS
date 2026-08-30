import type { ISODateString, UUID } from "./common";

/** Mirrors `app.core.tenancy.schemas.ConnectorSource`. */
export type ConnectorSource =
  | "slack"
  | "teams"
  | "github"
  | "azure_devops"
  | "jira"
  | "confluence"
  | "sharepoint"
  | "runbooks"
  | "google_drive"
  | "gitlab"
  | "notion"
  | "servicenow"
  | "pagerduty"
  | "monitoring";

/** Mirrors `app.core.tenancy.schemas.ConnectorStatus`. */
export type ConnectorStatus = "connecting" | "active" | "error" | "disconnected";

/** Mirrors `app.core.tenancy.schemas.ConnectorConfig` -- the real, persisted
 * connector row. `credential_ref` is deliberately omitted here: it is the
 * server-side envelope-encrypted credential reference, never a raw secret,
 * but there is still no reason for the frontend to hold or render it.
 */
export interface Connector {
  id: UUID;
  organizationId: UUID;
  projectId: UUID | null;
  source: ConnectorSource;
  config: Record<string, unknown>;
  status: ConnectorStatus;
  lastSyncedAt: ISODateString | null;
  createdAt: ISODateString;
  updatedAt: ISODateString;
}

export interface GithubRepoConfig {
  repo: string;
  ref?: string;
}

export interface CreateGithubConnectorInput {
  token: string;
  repos: GithubRepoConfig[];
}

export interface CreateSlackConnectorInput {
  token: string;
  channelIds: string[];
}

/** Mirrors `app.ingestion.connectors.jira`'s documented `ResolvedConnectorConfig.
 * config` shape exactly: `{"base_url": "...", "projects": ["OPS", "ENG"]}`.
 * `projects` is a list of Jira project *keys*, not display names. */
export interface CreateJiraConnectorInput {
  token: string;
  baseUrl: string;
  projects: string[];
}

/** Mirrors `app.ingestion.connectors.confluence`'s documented
 * `ResolvedConnectorConfig.config` shape: `{"base_url": "...", "spaces": ["ENG", "OPS"]}`.
 * `spaces` is a list of Confluence space *keys*, not display names. */
export interface CreateConfluenceConnectorInput {
  token: string;
  baseUrl: string;
  spaces: string[];
}

/** Mirrors `app.ingestion.connectors.teams`'s documented
 * `ResolvedConnectorConfig.config` shape: `{"team_id": "...", "channels": [...]}`.
 * `token` is an already-issued Graph API OAuth2 bearer access token (see
 * that module's docstring), not a long-lived credential this UI can obtain
 * itself. `team_id`/`channels` are Graph object IDs, not display names. */
export interface CreateTeamsConnectorInput {
  token: string;
  teamId: string;
  channels: string[];
}

/** Mirrors `app.ingestion.connectors.azure_devops`'s documented
 * `ResolvedConnectorConfig.config` shape: `{"organization": "...", "projects": [...]}`.
 * `token` is a literal Azure DevOps Personal Access Token. `projects` is a
 * list of project *names*, not IDs -- see that module's docstring. */
export interface CreateAzureDevOpsConnectorInput {
  token: string;
  organization: string;
  projects: string[];
}

/** Mirrors `app.ingestion.connectors.sharepoint`'s documented
 * `ResolvedConnectorConfig.config` shape: `{"site_ids": [...]}`. `token` is
 * an already-issued Graph API OAuth2 bearer access token, same as Teams --
 * there is no separate site base URL, unlike Jira/Confluence. */
export interface CreateSharePointConnectorInput {
  token: string;
  siteIds: string[];
}
