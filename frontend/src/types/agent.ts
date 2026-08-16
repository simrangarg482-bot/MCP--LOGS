// Matches `app.agents.schemas.AgentExecutionStats` field-for-field --
// `GET /observability/agents`'s real response. The previous shape invented
// `key`/`name`/`description`/`status` ("healthy"/"degraded"/"offline", a
// value the backend never produces)/`lastExecutionAt`/`executionsLast24h`
// and renamed `avg_latency_seconds` to a nonexistent `avgExecutionTimeMs`
// (different unit implied) -- none of it matched the real endpoint.
export interface AgentExecutionStats {
  agentName: string;
  executionCount: number;
  succeededCount: number;
  failedCount: number;
  avgConfidenceScore: number | null;
  avgLatencySeconds: number | null;
}

// Matches `app.shared.schemas.common.AgentExecutionStatus` exactly -- the
// real values are "succeeded"/"failed", not "success"/"failure".
export type AgentExecutionStatus = "running" | "succeeded" | "failed";
