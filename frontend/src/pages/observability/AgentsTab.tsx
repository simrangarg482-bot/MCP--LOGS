import { useQuery } from "@tanstack/react-query";
import { ArrowDown, Bot } from "lucide-react";
import { AgentStatusCard } from "@/components/domain/AgentStatus";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { listAgentStats } from "@/api/agents";
import { agentPipelineStages } from "@/mocks/data/agents";
import { sinceFor, type TimeWindow } from "@/utils/observability";

/**
 * Per-agent execution statistics (`GET /observability/agents`, `observability:read`), plus the static
 * reference of how a question is answered. This is what the standalone Agents page showed; it moved here
 * unchanged apart from the shared time window.
 */
export function AgentsTab({ window }: { window: TimeWindow }) {
  const statsQuery = useQuery({
    queryKey: ["observability", "agents", window],
    queryFn: () => listAgentStats(sinceFor(window)),
  });

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-ink-subtle">
          Agent execution stats (recorded runs for your organization)
        </p>
        {statsQuery.isLoading && <LoadingState label="Loading agent stats…" />}
        {statsQuery.isError && <ErrorState title="Could not load agent stats" onRetry={() => void statsQuery.refetch()} />}
        {statsQuery.data && statsQuery.data.length === 0 && (
          <EmptyState
            icon={Bot}
            title="No agent executions recorded yet"
            description={
              window === "all"
                ? "Statistics appear here once EKIP answers a question, triages an incident or runs another agent for your organization."
                : "No agent executions in this time window. Try a longer window."
            }
          />
        )}
        {statsQuery.data && statsQuery.data.length > 0 && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="agent-stats">
            {statsQuery.data.map((agent) => (
              <AgentStatusCard key={agent.agentName} agent={agent} />
            ))}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-border bg-surface p-5 shadow-subtle">
        <p className="mb-4 text-xs font-medium uppercase tracking-wide text-ink-subtle">
          How a question is answered (static reference, not live per-stage monitoring)
        </p>
        <div className="flex flex-col items-center gap-1">
          {agentPipelineStages.map((stage, index) => (
            <div key={stage.key} className="flex flex-col items-center gap-1">
              <div className="w-full max-w-xs rounded-md border border-border bg-white/[0.03] px-4 py-2.5 text-center">
                <p className="text-sm font-medium text-ink">{stage.name}</p>
              </div>
              {index < agentPipelineStages.length - 1 && <ArrowDown className="h-4 w-4 text-ink-subtle" />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
