import { useQuery } from "@tanstack/react-query";
import { ArrowDown } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { AgentStatusCard } from "@/components/domain/AgentStatus";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { listAgentStats } from "@/api/agents";
import { agentPipelineStages } from "@/mocks/data/agents";

export function AgentsPage() {
  const statsQuery = useQuery({ queryKey: ["agents", "stats"], queryFn: listAgentStats });

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Agents"
        description="EKIP's retrieval and reasoning pipeline, from query understanding to grounded answers."
      />

      <div className="rounded-lg border border-border bg-surface p-5 shadow-subtle">
        <p className="mb-4 text-xs font-medium uppercase tracking-wide text-ink-subtle">
          How a question is answered (static reference, not live per-stage monitoring)
        </p>
        <div className="flex flex-col items-center gap-1">
          {agentPipelineStages.map((stage, index) => (
            <div key={stage.key} className="flex flex-col items-center gap-1">
              <div className="w-full max-w-xs rounded-md border border-border bg-slate-50 px-4 py-2.5 text-center">
                <p className="text-sm font-medium text-ink">{stage.name}</p>
              </div>
              {index < agentPipelineStages.length - 1 && (
                <ArrowDown className="h-4 w-4 text-ink-subtle" />
              )}
            </div>
          ))}
        </div>
      </div>

      <div>
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-ink-subtle">
          Agent execution stats (real data, `agent_executions`)
        </p>
        {statsQuery.isLoading && <LoadingState label="Loading agent stats…" />}
        {statsQuery.isError && <ErrorState onRetry={() => statsQuery.refetch()} />}
        {statsQuery.data && statsQuery.data.length === 0 && (
          <EmptyState title="No agent executions recorded yet" />
        )}
        {statsQuery.data && statsQuery.data.length > 0 && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {statsQuery.data.map((agent) => (
              <AgentStatusCard key={agent.agentName} agent={agent} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
