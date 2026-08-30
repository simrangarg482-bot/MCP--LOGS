import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  Connector,
  ConnectorSource,
  CreateAzureDevOpsConnectorInput,
  CreateConfluenceConnectorInput,
  CreateGithubConnectorInput,
  CreateJiraConnectorInput,
  CreateSharePointConnectorInput,
  CreateSlackConnectorInput,
  CreateTeamsConnectorInput,
} from "@/types/connector";
import { mockConnectors } from "@/mocks/data/connectors";

export async function listConnectors(): Promise<Connector[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockConnectors);
  }
  return apiRequest<Connector[]>(`/tenancy/connectors`);
}

export async function createEnterpriseConnector(
  source: Extract<ConnectorSource, "google_drive" | "gitlab" | "notion" | "servicenow" | "pagerduty">,
  token: string,
  config: Record<string, unknown>,
): Promise<Connector> {
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: { source, credentialRef: token, config },
  });
}

export async function createGithubConnector(input: CreateGithubConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[0],
        id: `conn-github-${Date.now()}`,
        source: "github",
        status: "connecting",
        config: { repos: input.repos },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "github",
      credentialRef: input.token,
      config: { repos: input.repos },
    },
  });
}

export async function createSlackConnector(input: CreateSlackConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[1],
        id: `conn-slack-${Date.now()}`,
        source: "slack",
        status: "connecting",
        config: { channels: input.channelIds },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "slack",
      credentialRef: input.token,
      config: { channels: input.channelIds },
    },
  });
}

export async function createJiraConnector(input: CreateJiraConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[0],
        id: `conn-jira-${Date.now()}`,
        source: "jira",
        status: "connecting",
        config: { baseUrl: input.baseUrl, projects: input.projects },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "jira",
      credentialRef: input.token,
      config: { baseUrl: input.baseUrl, projects: input.projects },
    },
  });
}

export async function createConfluenceConnector(input: CreateConfluenceConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[0],
        id: `conn-confluence-${Date.now()}`,
        source: "confluence",
        status: "connecting",
        config: { baseUrl: input.baseUrl, spaces: input.spaces },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "confluence",
      credentialRef: input.token,
      config: { baseUrl: input.baseUrl, spaces: input.spaces },
    },
  });
}

export async function createTeamsConnector(input: CreateTeamsConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[4],
        id: `conn-teams-${Date.now()}`,
        source: "teams",
        status: "connecting",
        config: { teamId: input.teamId, channels: input.channels },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "teams",
      credentialRef: input.token,
      config: { team_id: input.teamId, channels: input.channels },
    },
  });
}

export async function createAzureDevOpsConnector(input: CreateAzureDevOpsConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[0],
        id: `conn-azure-devops-${Date.now()}`,
        source: "azure_devops",
        status: "connecting",
        config: { organization: input.organization, projects: input.projects },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "azure_devops",
      credentialRef: input.token,
      config: { organization: input.organization, projects: input.projects },
    },
  });
}

export async function createSharePointConnector(input: CreateSharePointConnectorInput): Promise<Connector> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      {
        ...mockConnectors[0],
        id: `conn-sharepoint-${Date.now()}`,
        source: "sharepoint",
        status: "connecting",
        config: { siteIds: input.siteIds },
      },
      400,
    );
  }
  return apiRequest<Connector>(`/tenancy/connectors`, {
    method: "POST",
    body: {
      source: "sharepoint",
      credentialRef: input.token,
      config: { site_ids: input.siteIds },
    },
  });
}

export async function triggerConnectorSync(connectorId: string): Promise<{ status: string }> {
  if (USE_MOCK_DATA) {
    return mockDelay({ status: "enqueued" }, 400);
  }
  return apiRequest<{ status: string }>(`/tenancy/connectors/${connectorId}/sync`, { method: "POST" });
}

/**
 * `DELETE /tenancy/connectors/{id}` -- a real `DELETE` verb backed by a
 * status change (`"disconnected"`), not a dropped row: `ingestion_jobs.
 * connector_config_id` is `ON DELETE RESTRICT`, so the backend can't hard-
 * delete a connector that has ever synced. See `core.tenancy.service.
 * disconnect_connector`'s own docstring. The row survives server-side;
 * `listConnectors`'s caller filters `"disconnected"` out of the visible
 * list (see `ConnectorsPage.tsx`), so this reads as deletion here.
 */
export async function deleteConnector(connectorId: string): Promise<Connector> {
  if (USE_MOCK_DATA) {
    const existing = mockConnectors.find((c) => c.id === connectorId) ?? mockConnectors[0];
    return mockDelay({ ...existing, status: "disconnected" }, 300);
  }
  return apiRequest<Connector>(`/tenancy/connectors/${connectorId}`, { method: "DELETE" });
}
