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
  // Phase 5.7 -- `null` (not `0`) whenever no execution in this group
  // captured usage at all; see `AgentExecutionStats`'s own docstring.
  totalPromptTokens: number | null;
  totalCompletionTokens: number | null;
  totalTokens: number | null;
  // An estimate from published pricing, not real billing data -- see
  // `app.agents.telemetry.get_estimated_cost_usd`'s own docstring.
  estimatedCostUsd: number | null;
}
