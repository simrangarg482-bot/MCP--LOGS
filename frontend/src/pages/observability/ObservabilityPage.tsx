import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { TabPanel, Tabs } from "@/components/ui/Tabs";
import { useAuth } from "@/context/AuthContext";
import {
  TIME_WINDOWS,
  isObservabilityTab,
  isTimeWindow,
  type ObservabilityTab,
  type TimeWindow,
} from "@/utils/observability";
import { AgentsTab } from "./AgentsTab";
import { IngestionTab } from "./IngestionTab";
import { McpToolsTab } from "./McpToolsTab";
import { StorageTab } from "./StorageTab";

const TAB_ITEMS = [
  { key: "agents", label: "Agents" },
  { key: "mcp", label: "MCP Tools" },
  { key: "ingestion", label: "Ingestion" },
  { key: "storage", label: "Storage" },
];

/**
 * Tenant-level monitoring: agents, MCP tools and ingestion for the signed-in organization only (the
 * endpoints take the organization from the caller's identity and are row-level secured). Needs
 * `observability:read`. The check below is UX only: the backend re-checks the permission on every call, and
 * no request is made at all without it.
 */
export function ObservabilityPage() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const fetching = useIsFetching({ queryKey: ["observability"] }) > 0;

  const allowed = Boolean(user?.permissions.includes("observability:read"));
  const tabParam = params.get("tab");
  const tab: ObservabilityTab = isObservabilityTab(tabParam) ? tabParam : "agents";
  const windowParam = params.get("window");
  const window: TimeWindow = isTimeWindow(windowParam) ? windowParam : "all";

  function update(next: { tab?: ObservabilityTab; window?: TimeWindow }) {
    const merged = new URLSearchParams(params);
    if (next.tab) merged.set("tab", next.tab);
    if (next.window) merged.set("window", next.window);
    setParams(merged, { replace: true });
  }

  if (!allowed) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Observability" description="Agents, MCP tools and ingestion for your organization." />
        <p
          role="alert"
          className="rounded-md border border-border bg-white/[0.03] px-3 py-2 text-xs text-ink-muted"
          data-testid="observability-denied"
        >
          You need the <code className="font-mono">observability:read</code> permission to view this page. Contact an
          organization administrator if you believe this is a mistake.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Observability"
        description="How agents, MCP tools and ingestion are performing for your organization."
        actions={
          <>
            <div className="w-44">
              <label htmlFor="observability-window" className="sr-only">
                Time window
              </label>
              <Select
                id="observability-window"
                value={window}
                onChange={(e) => update({ window: e.target.value as TimeWindow })}
              >
                {TIME_WINDOWS.map((w) => (
                  <option key={w.value} value={w.value}>
                    {w.label}
                  </option>
                ))}
              </Select>
            </div>
            <Button
              onClick={() => void queryClient.invalidateQueries({ queryKey: ["observability"] })}
              isLoading={fetching}
              aria-label="Refresh observability data"
            >
              {!fetching && <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}
              Refresh
            </Button>
          </>
        }
      />

      <Tabs items={TAB_ITEMS} activeKey={tab} onChange={(key) => update({ tab: key as ObservabilityTab })} idPrefix="observability" />

      <TabPanel idPrefix="observability" tabKey={tab}>
        {tab === "agents" && <AgentsTab window={window} />}
        {tab === "mcp" && <McpToolsTab window={window} />}
        {tab === "ingestion" && <IngestionTab window={window} />}
        {tab === "storage" && <StorageTab />}
      </TabPanel>
    </div>
  );
}
