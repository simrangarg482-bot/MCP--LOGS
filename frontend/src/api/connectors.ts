import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  Connector,
  CreateConfluenceConnectorInput,
  CreateGithubConnectorInput,
  CreateJiraConnectorInput,
  CreateSlackConnectorInput,
} from "@/types/connector";
import { mockConnectors } from "@/mocks/data/connectors";

export async function listConnectors(): Promise<Connector[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockConnectors);
  }
  return apiRequest<Connector[]>(`/tenancy/connectors`);
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
