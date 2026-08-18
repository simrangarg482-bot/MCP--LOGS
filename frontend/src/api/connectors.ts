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
