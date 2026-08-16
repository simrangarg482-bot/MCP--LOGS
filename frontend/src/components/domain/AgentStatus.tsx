import type { AgentExecutionStats } from "@/types/agent";
import { formatPercent } from "@/utils/format";

interface AgentStatusProps {
  agent: AgentExecutionStats;
}

// Renders only real fields from `GET /observability/agents`
// (`app.agents.schemas.AgentExecutionStats`) -- no `status`/`lastExecutionAt`
// exist on the backend, so this shows a derived (client-computed, not
// invented) success rate from the real `succeededCount`/`executionCount`
// instead of a fictional health enum.
export function AgentStatusCard({ agent }: AgentStatusProps) {
  const successRate = agent.executionCount > 0 ? agent.succeededCount / agent.executionCount : null;

  return (
    <div className="flex w-full flex-col gap-2.5 rounded-lg border border-border bg-surface px-4 py-3.5 shadow-subtle">
      <p className="text-sm font-semibold text-ink">{agent.agentName}</p>
      <dl className="mt-1 grid grid-cols-2 gap-2 text-xs">
        <div>
          <dt className="text-ink-subtle">Executions</dt>
          <dd className="font-medium text-ink">{agent.executionCount}</dd>
        </div>
        <div>
          <dt className="text-ink-subtle">Success rate</dt>
          <dd className="font-medium text-ink">{successRate !== null ? formatPercent(successRate) : "—"}</dd>
        </div>
        <div>
          <dt className="text-ink-subtle">Avg confidence</dt>
          <dd className="font-medium text-ink">
            {agent.avgConfidenceScore !== null ? formatPercent(agent.avgConfidenceScore) : "n/a"}
          </dd>
        </div>
        <div>
          <dt className="text-ink-subtle">Avg latency</dt>
          <dd className="font-medium text-ink">
            {agent.avgLatencySeconds !== null ? `${agent.avgLatencySeconds.toFixed(1)}s` : "—"}
          </dd>
        </div>
      </dl>
      {agent.failedCount > 0 && (
        <p className="text-xs text-critical">{agent.failedCount} failed execution{agent.failedCount === 1 ? "" : "s"}</p>
      )}
    </div>
  );
}
