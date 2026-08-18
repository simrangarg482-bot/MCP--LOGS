import { useQuery } from "@tanstack/react-query";
import { Wrench } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { StatusBadge } from "@/components/data/StatusBadge";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { listMcpTools } from "@/api/mcp";
import { formatDurationMs } from "@/utils/format";
import { formatRelativeTime } from "@/utils/date";

export function McpToolsPage() {
  const toolsQuery = useQuery({ queryKey: ["mcp", "tools"], queryFn: listMcpTools });

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="MCP Tools"
        description="Model Context Protocol tools EKIP's agents use to search, retrieve, and analyze."
      />

      {toolsQuery.isLoading && <LoadingState label="Loading MCP tools…" />}
      {toolsQuery.isError && <ErrorState onRetry={() => toolsQuery.refetch()} />}
      {toolsQuery.data && toolsQuery.data.length === 0 && (
        <EmptyState icon={Wrench} title="No MCP tools registered" />
      )}

      {toolsQuery.data && toolsQuery.data.length > 0 && (
        <div className="flex flex-col gap-3">
          {toolsQuery.data.map((tool) => (
            <Card key={tool.name}>
              <CardHeader>
                <div className="min-w-0 break-words">
                  <CardTitle>
                    <code className="text-sm">{tool.name}</code>
                  </CardTitle>
                  {tool.description && (
                    <p className="mt-1 max-w-2xl text-xs text-ink-muted">{tool.description}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  {tool.status && (
                    <StatusBadge status={tool.status === "available" ? "active" : tool.status === "deprecated" ? "error" : "disconnected"} />
                  )}
                </div>
              </CardHeader>
              <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-ink-muted">
                {tool.parameters && <span>Parameters: {tool.parameters.length}</span>}
                {tool.avgLatencyMs !== undefined && <span>Avg latency: {formatDurationMs(tool.avgLatencyMs)}</span>}
                {tool.maxLatencyMs !== undefined && <span>Max latency: {formatDurationMs(tool.maxLatencyMs)}</span>}
                {tool.callCountLast24h !== undefined && <span>Calls: {tool.callCountLast24h.toLocaleString()}</span>}
                {tool.errorCount !== undefined && <span>Errors: {tool.errorCount.toLocaleString()}</span>}
                {tool.lastExecutionAt && <span>Last run: {formatRelativeTime(tool.lastExecutionAt)}</span>}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
