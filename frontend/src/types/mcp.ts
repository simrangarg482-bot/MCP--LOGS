import type { ISODateString } from "./common";

export type McpParamType = "string" | "number" | "boolean" | "object" | "array";

export interface McpToolParameter {
  name: string;
  type: McpParamType;
  required: boolean;
  description: string;
  defaultValue?: string;
}

export type McpToolStatus = "available" | "unavailable" | "deprecated";

/**
 * `description`/`status`/`parameters` are optional, not required: they only
 * exist in mock mode's illustrative tool catalog (`mocks/data/mcp.ts`). The
 * real `GET /observability/mcp` endpoint returns usage *statistics*
 * (request/error counts, latency), not a tool catalog with parameter
 * schemas — the backend has no such introspection endpoint. Keeping these
 * fields optional (rather than fabricating placeholder values for real
 * responses) is what lets `McpToolsPage` render real data honestly instead
 * of crashing on `undefined` or inventing fake catalog details.
 */
export interface McpTool {
  name: string;
  description?: string;
  status?: McpToolStatus;
  parameters?: McpToolParameter[];
  lastExecutionAt?: ISODateString;
  avgLatencyMs?: number;
  callCountLast24h?: number;
  /** Real fields from `GET /observability/mcp` -- undefined in mock mode. */
  errorCount?: number;
  maxLatencyMs?: number;
}
