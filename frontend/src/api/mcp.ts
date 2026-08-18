import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { McpTool } from "@/types/mcp";
import { mockMcpTools } from "@/mocks/data/mcp";

/**
 * Matches `app.core.observability.schemas.McpToolStats` field-for-field
 * (after `apiRequest`'s automatic snake_case -> camelCase conversion) --
 * `GET /observability/mcp`'s real response shape. This is a usage-*stats*
 * aggregate, not a tool catalog: there is no real endpoint that returns
 * tool descriptions/parameter schemas/status (those only exist in mock
 * mode's illustrative `mockMcpTools`, per `McpTool`'s own doc comment) --
 * a previous version of this function returned `McpTool[]` directly for
 * real responses too, which silently didn't match at all (`tool_name` vs
 * `name`, no `parameters`/`status` field on the real response whatsoever).
 */
interface McpToolStatsResponse {
  toolName: string;
  requestCount: number;
  errorCount: number;
  avgLatencyMs: number | null;
  maxLatencyMs: number | null;
}

export async function listMcpTools(): Promise<McpTool[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockMcpTools);
  }
  const stats = await apiRequest<McpToolStatsResponse[]>(`/observability/mcp`);
  // Maps only the fields that genuinely exist on the real response --
  // `description`/`status`/`parameters` are left undefined (not fabricated
  // placeholders), which `McpToolsPage` renders conditionally.
  return stats.map((stat) => ({
    name: stat.toolName,
    avgLatencyMs: stat.avgLatencyMs ?? undefined,
    callCountLast24h: stat.requestCount,
    errorCount: stat.errorCount,
    maxLatencyMs: stat.maxLatencyMs ?? undefined,
  }));
}
