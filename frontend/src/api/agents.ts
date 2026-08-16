import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { AgentExecutionStats } from "@/types/agent";
import { mockAgentStats } from "@/mocks/data/agents";

/**
 * `GET /observability/agents` (`app.agents.service.get_agent_execution_stats`,
 * permission `observability:read`) -- real per-agent aggregate stats.
 *
 * There is no real per-agent execution-*list* endpoint (`GET /observability/
 * agents/executions` was a previous, fictional call to a route that doesn't
 * exist) -- the closest real thing, `GET /ask/history`, is a single user's
 * own question history, not a per-agent execution list, so it isn't a
 * substitute and this function was removed rather than pointed at it.
 */
export async function listAgentStats(): Promise<AgentExecutionStats[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockAgentStats);
  }
  return apiRequest<AgentExecutionStats[]>(`/observability/agents`);
}
