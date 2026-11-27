import { useQuery } from "@tanstack/react-query";
import { Wrench } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { listMcpToolCatalogue, listMcpToolUsage } from "@/api/mcp";
import { formatDurationMs } from "@/utils/format";
import { formatRelativeTime } from "@/utils/date";
import { sinceFor, type TimeWindow } from "@/utils/observability";
import type { McpToolUsage } from "@/types/mcp";

function UsageLine({ usage, emptyText }: { usage: McpToolUsage | undefined; emptyText: string }) {
  if (!usage) {
    return <span>{emptyText}</span>;
  }
  return (
    <>
      <span>Calls: {usage.requestCount.toLocaleString()}</span>
      <span>Errors: {usage.errorCount.toLocaleString()}</span>
      {usage.avgLatencyMs !== null && <span>Avg latency: {formatDurationMs(usage.avgLatencyMs)}</span>}
      {usage.maxLatencyMs !== null && <span>Max latency: {formatDurationMs(usage.maxLatencyMs)}</span>}
    </>
  );
}

/**
 * The registered MCP tools with their inputs (`GET /observability/mcp/tools`) and, separately, recorded
 * usage for the caller's organization (`GET /observability/mcp`). This is what the standalone MCP Tools
 * page showed, with the usage figures following the shared time window. The catalogue is not time-based.
 */
export function McpToolsTab({ window }: { window: TimeWindow }) {
  const catalogueQuery = useQuery({ queryKey: ["observability", "mcp", "catalogue"], queryFn: () => listMcpToolCatalogue() });
  // Usage is secondary: if it fails the catalogue must still render.
  const usageQuery = useQuery({
    queryKey: ["observability", "mcp", "usage", window],
    queryFn: () => listMcpToolUsage(sinceFor(window)),
  });

  const catalogue = catalogueQuery.data ?? [];
  const usageByTool = new Map((usageQuery.data ?? []).map((usage) => [usage.toolName, usage]));
  const registeredNames = new Set(catalogue.map((tool) => tool.name));
  const otherActivity = (usageQuery.data ?? []).filter((usage) => !registeredNames.has(usage.toolName));
  const syncedAt = catalogue.length > 0 ? catalogue[0].syncedAt : undefined;
  const noCalls = window === "all" ? "No recorded calls from your organization." : "No recorded calls in this time window.";

  return (
    <div className="flex flex-col gap-5">
      <p className="text-xs text-ink-muted">
        The tools the EKIP MCP server has registered, with their inputs. Usage figures are recorded separately for your
        organization.
      </p>

      {catalogueQuery.isLoading && <LoadingState label="Loading MCP tools…" />}
      {catalogueQuery.isError && <ErrorState title="Could not load MCP tools" onRetry={() => void catalogueQuery.refetch()} />}
      {catalogueQuery.data && catalogue.length === 0 && (
        <EmptyState
          icon={Wrench}
          title="No MCP tools published yet"
          description="The MCP server publishes its registered tools when it starts."
        />
      )}

      {catalogue.length > 0 && (
        <section aria-labelledby="mcp-catalogue-heading" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="mcp-catalogue-heading" className="text-sm font-semibold text-ink">
              Registered tools ({catalogue.length})
            </h2>
            {syncedAt && (
              <span className="text-xs text-ink-muted">Published by the MCP server {formatRelativeTime(syncedAt)}</span>
            )}
          </div>

          {catalogue.map((tool) => (
            <Card key={tool.name} data-testid={`mcp-tool-${tool.name}`}>
              <CardHeader>
                <div className="min-w-0 break-words">
                  <CardTitle>
                    <code className="text-sm">{tool.name}</code>
                  </CardTitle>
                  <p className="mt-1 max-w-3xl whitespace-pre-line text-xs text-ink-muted">{tool.description}</p>
                </div>
                <Badge tone="success">Available</Badge>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {tool.parameters.length === 0 ? (
                  <p className="text-xs text-ink-muted">Takes no parameters.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <caption className="sr-only">Input parameters of {tool.name}</caption>
                      <thead className="text-ink-muted">
                        <tr>
                          <th className="py-1 pr-3 font-medium">Parameter</th>
                          <th className="py-1 pr-3 font-medium">Type</th>
                          <th className="py-1 pr-3 font-medium">Required</th>
                          <th className="py-1 font-medium">Default / description</th>
                        </tr>
                      </thead>
                      <tbody>
                        {tool.parameters.map((param) => (
                          <tr key={param.name} className="border-t border-white/10 align-top">
                            <td className="py-1.5 pr-3">
                              <code>{param.name}</code>
                            </td>
                            <td className="py-1.5 pr-3">{param.type}</td>
                            <td className="py-1.5 pr-3">{param.required ? "required" : "optional"}</td>
                            <td className="py-1.5 text-ink-muted">
                              {param.default !== null && <code className="mr-2">default: {param.default}</code>}
                              {param.description}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div
                  className="flex flex-wrap items-center gap-x-6 gap-y-1 border-t border-white/10 pt-2 text-xs text-ink-muted"
                  aria-label={`Recorded usage of ${tool.name}`}
                >
                  <span className="font-medium text-ink">Recorded usage:</span>
                  {usageQuery.isError ? (
                    <span>Usage statistics are unavailable.</span>
                  ) : (
                    <UsageLine usage={usageByTool.get(tool.name)} emptyText={noCalls} />
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </section>
      )}

      {otherActivity.length > 0 && (
        <section aria-labelledby="mcp-other-heading" className="flex flex-col gap-2">
          <h2 id="mcp-other-heading" className="text-sm font-semibold text-ink">
            Other recorded activity
          </h2>
          <p className="text-xs text-ink-muted">
            Calls recorded for your organization that do not match a currently registered tool (for example resource
            reads, or a tool that has since been removed).
          </p>
          <Card>
            <CardContent className="flex flex-col gap-1 text-xs text-ink-muted">
              {otherActivity.map((usage) => (
                <div key={usage.toolName} className="flex flex-wrap items-center gap-x-6 gap-y-1">
                  <code className="text-ink">{usage.toolName}</code>
                  <UsageLine usage={usage} emptyText="" />
                </div>
              ))}
            </CardContent>
          </Card>
        </section>
      )}
    </div>
  );
}
