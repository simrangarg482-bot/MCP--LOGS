import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Plug, Plus } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { ConnectorCard } from "@/components/domain/ConnectorCard";
import { ConnectConnectorModal } from "@/components/domain/ConnectConnectorModal";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { Drawer } from "@/components/ui/Drawer";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { StatusBadge } from "@/components/data/StatusBadge";
import {
  createAzureDevOpsConnector,
  createEnterpriseConnector,
  createConfluenceConnector,
  createGithubConnector,
  createJiraConnector,
  createSharePointConnector,
  createSlackConnector,
  createTeamsConnector,
  deleteConnector,
  listConnectors,
  triggerConnectorSync,
} from "@/api/connectors";
import { listIngestionRuns, replayIngestionRun } from "@/api/ingestion";
import type { Connector } from "@/types/connector";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatDateTime, formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";
import { SearchBar } from "@/components/data/SearchBar";

export function ConnectorsPage() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  // Mirrors the real gate `core.tenancy.service.register_connector`/
  // `get_connector` (used by sync) enforce -- UX only, the backend
  // re-checks regardless.
  const canManage = Boolean(user?.permissions.includes("tenancy:manage"));
  const [viewing, setViewing] = useState<Connector | null>(null);
  const [isConnectOpen, setIsConnectOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Connector | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "error">("all");

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: listConnectors,
    // A connector remains `connecting` until its ingestion job commits the
    // final page. Poll only during that transitional state so the badge
    // becomes `active` without a manual refresh, while avoiding permanent
    // background traffic once every connector is settled.
    refetchInterval: (query) =>
      query.state.data?.some((connector) => connector.status === "connecting") ? 5_000 : false,
  });
  // `disconnect_connector` (backend) is a status change, not a dropped row
  // -- `ingestion_jobs.connector_config_id` is `ON DELETE RESTRICT`, so a
  // hard delete isn't possible for any connector that's ever synced. This
  // filters "disconnected" rows out of the visible grid so deleting one
  // reads as deletion here, while its job/document history stays intact
  // server-side for anyone with direct database/audit-log access.
  const configuredConnectors = connectorsQuery.data?.filter((connector) => connector.status !== "disconnected") ?? [];
  const visibleConnectors = configuredConnectors.filter((connector) => {
    const matchesSearch = titleCase(connector.source).toLowerCase().includes(search.trim().toLowerCase());
    const matchesStatus = statusFilter === "all" || connector.status === statusFilter;
    return matchesSearch && matchesStatus;
  });
  const activeCount = configuredConnectors.filter((connector) => connector.status === "active").length;
  const errorCount = configuredConnectors.filter((connector) => connector.status === "error").length;

  const runsQuery = useQuery({
    queryKey: ["ingestion-runs", viewing?.id],
    queryFn: () => listIngestionRuns(viewing!.id),
    enabled: Boolean(viewing),
    refetchInterval: viewing ? 5_000 : false,
  });

  const syncMutation = useMutation({
    mutationFn: (connector: Connector) => triggerConnectorSync(connector.id),
    onSuccess: (_, connector) => {
      toast({ variant: "info", title: `Sync started for ${titleCase(connector.source)}` });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: (_, connector) => {
      toast({ variant: "error", title: `Failed to sync ${titleCase(connector.source)}` });
    },
  });

  const replayMutation = useMutation({
    mutationFn: ({ connectorId, jobId }: { connectorId: string; jobId: string }) =>
      replayIngestionRun(connectorId, jobId),
    onSuccess: () => {
      toast({ variant: "info", title: "Dead-lettered ingestion run queued for replay" });
      queryClient.invalidateQueries({ queryKey: ["ingestion-runs", viewing?.id] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to replay ingestion run" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (connector: Connector) => deleteConnector(connector.id),
    onSuccess: (_, connector) => {
      toast({ variant: "success", title: `${titleCase(connector.source)} connector deleted` });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
      setDeleteTarget(null);
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to delete connector" });
      setDeleteTarget(null);
    },
  });

  const githubMutation = useMutation({
    mutationFn: ({ token, repos }: { token: string; repos: { repo: string; ref?: string }[] }) =>
      createGithubConnector({ token, repos }),
    onSuccess: () => {
      toast({ variant: "success", title: "GitHub connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add GitHub connector" });
    },
  });

  const slackMutation = useMutation({
    mutationFn: ({ token, channelIds }: { token: string; channelIds: string[] }) =>
      createSlackConnector({ token, channelIds }),
    onSuccess: () => {
      toast({ variant: "success", title: "Slack connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add Slack connector" });
    },
  });

  const jiraMutation = useMutation({
    mutationFn: ({ token, baseUrl, projects }: { token: string; baseUrl: string; projects: string[] }) =>
      createJiraConnector({ token, baseUrl, projects }),
    onSuccess: () => {
      toast({ variant: "success", title: "Jira connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add Jira connector" });
    },
  });

  const confluenceMutation = useMutation({
    mutationFn: ({ token, baseUrl, spaces }: { token: string; baseUrl: string; spaces: string[] }) =>
      createConfluenceConnector({ token, baseUrl, spaces }),
    onSuccess: () => {
      toast({ variant: "success", title: "Confluence connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add Confluence connector" });
    },
  });

  const teamsMutation = useMutation({
    mutationFn: ({ token, teamId, channels }: { token: string; teamId: string; channels: string[] }) =>
      createTeamsConnector({ token, teamId, channels }),
    onSuccess: () => {
      toast({ variant: "success", title: "Teams connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add Teams connector" });
    },
  });

  const azureDevOpsMutation = useMutation({
    mutationFn: ({ token, organization, projects }: { token: string; organization: string; projects: string[] }) =>
      createAzureDevOpsConnector({ token, organization, projects }),
    onSuccess: () => {
      toast({ variant: "success", title: "Azure DevOps connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add Azure DevOps connector" });
    },
  });

  const sharePointMutation = useMutation({
    mutationFn: ({ token, siteIds }: { token: string; siteIds: string[] }) =>
      createSharePointConnector({ token, siteIds }),
    onSuccess: () => {
      toast({ variant: "success", title: "SharePoint connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to add SharePoint connector" });
    },
  });

  const enterpriseMutation = useMutation({
    mutationFn: ({ source, token, config }: { source: "google_drive" | "gitlab" | "notion" | "servicenow" | "pagerduty"; token: string; config: Record<string, unknown> }) =>
      createEnterpriseConnector(source, token, config),
    onSuccess: () => {
      toast({ variant: "success", title: "Enterprise connector added" });
      queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => toast({ variant: "error", title: "Failed to add enterprise connector" }),
  });

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Connectors"
        description="Integrations that feed knowledge and incident context into EKIP."
        actions={
          <Button
            variant="primary"
            className="gap-1.5"
            onClick={() => setIsConnectOpen(true)}
            disabled={!canManage}
            title={canManage ? undefined : "Requires the tenancy:manage permission"}
          >
            <Plus className="h-4 w-4" />
            Connect a source
          </Button>
        }
      />

      {connectorsQuery.data && configuredConnectors.length > 0 && (
        <>
          <div className="grid grid-cols-3 gap-3">
            <button type="button" onClick={() => setStatusFilter("all")} className={`rounded-xl border bg-white p-3 text-left shadow-subtle ${statusFilter === "all" ? "border-accent" : "border-border"}`}>
              <span className="flex items-center gap-2 text-xs text-ink-muted"><Plug className="h-3.5 w-3.5" />Configured</span><span className="mt-1 block text-xl font-semibold text-ink">{configuredConnectors.length}</span>
            </button>
            <button type="button" onClick={() => setStatusFilter("active")} className={`rounded-xl border bg-white p-3 text-left shadow-subtle ${statusFilter === "active" ? "border-success" : "border-border"}`}>
              <span className="flex items-center gap-2 text-xs text-ink-muted"><CheckCircle2 className="h-3.5 w-3.5 text-success" />Active</span><span className="mt-1 block text-xl font-semibold text-ink">{activeCount}</span>
            </button>
            <button type="button" onClick={() => setStatusFilter("error")} className={`rounded-xl border bg-white p-3 text-left shadow-subtle ${statusFilter === "error" ? "border-critical" : "border-border"}`}>
              <span className="flex items-center gap-2 text-xs text-ink-muted"><AlertTriangle className="h-3.5 w-3.5 text-critical" />Needs attention</span><span className="mt-1 block text-xl font-semibold text-ink">{errorCount}</span>
            </button>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <SearchBar value={search} onChange={setSearch} placeholder="Find a connected source…" className="w-full sm:max-w-sm" />
            <p className="text-xs text-ink-muted">Showing {visibleConnectors.length} of {configuredConnectors.length} sources</p>
          </div>
        </>
      )}

      {connectorsQuery.isLoading && <LoadingState label="Loading connectors…" />}
      {connectorsQuery.isError && <ErrorState onRetry={() => connectorsQuery.refetch()} />}
      {connectorsQuery.data && visibleConnectors.length === 0 && (
        <EmptyState
          icon={Plug}
          title={configuredConnectors.length === 0 ? "No connectors configured" : "No connectors match this view"}
          description={configuredConnectors.length === 0 ? "Connect an engineering source to start building searchable, evidence-backed knowledge." : "Try another search term or select a different status."}
          action={
            canManage && configuredConnectors.length === 0 ? (
              <Button variant="primary" className="gap-1.5" onClick={() => setIsConnectOpen(true)}>
                <Plus className="h-4 w-4" />
                Connect a source
              </Button>
            ) : undefined
          }
        />
      )}

      {connectorsQuery.data && visibleConnectors.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {visibleConnectors.map((connector) => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              onView={setViewing}
              onSync={(c) => syncMutation.mutate(c)}
              onDelete={setDeleteTarget}
              isSyncing={syncMutation.isPending && syncMutation.variables?.id === connector.id}
              canManage={canManage}
            />
          ))}
        </div>
      )}

      <ConnectConnectorModal
        open={isConnectOpen}
        onClose={() => setIsConnectOpen(false)}
        isSubmitting={
          githubMutation.isPending ||
          slackMutation.isPending ||
          jiraMutation.isPending ||
          confluenceMutation.isPending ||
          teamsMutation.isPending ||
          azureDevOpsMutation.isPending ||
          sharePointMutation.isPending
          || enterpriseMutation.isPending
        }
        onSubmitGithub={async (token, repos) => {
          await githubMutation.mutateAsync({ token, repos });
        }}
        onSubmitSlack={async (token, channelIds) => {
          await slackMutation.mutateAsync({ token, channelIds });
        }}
        onSubmitJira={async (token, baseUrl, projects) => {
          await jiraMutation.mutateAsync({ token, baseUrl, projects });
        }}
        onSubmitConfluence={async (token, baseUrl, spaces) => {
          await confluenceMutation.mutateAsync({ token, baseUrl, spaces });
        }}
        onSubmitTeams={async (token, teamId, channels) => {
          await teamsMutation.mutateAsync({ token, teamId, channels });
        }}
        onSubmitAzureDevOps={async (token, organization, projects) => {
          await azureDevOpsMutation.mutateAsync({ token, organization, projects });
        }}
        onSubmitSharePoint={async (token, siteIds) => {
          await sharePointMutation.mutateAsync({ token, siteIds });
        }}
        onSubmitEnterprise={async (source, token, config) => {
          await enterpriseMutation.mutateAsync({ source, token, config });
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this connector?"
        description={
          deleteTarget
            ? `${titleCase(deleteTarget.source)} will stop syncing and disappear from this list. Already-ingested knowledge from it is not removed. This can't be undone from here -- you'd need to reconnect it from scratch.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        isLoading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />

      <Drawer open={Boolean(viewing)} onClose={() => setViewing(null)} title={viewing ? titleCase(viewing.source) : ""}>
        {viewing && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-muted">Status</span>
              <StatusBadge status={viewing.status} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-muted">Connected</span>
              <span className="text-sm text-ink">{formatDateTime(viewing.createdAt)}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-muted">Last sync</span>
              <span className="text-sm text-ink">
                {viewing.lastSyncedAt ? formatRelativeTime(viewing.lastSyncedAt) : "Never"}
              </span>
            </div>
            <div>
              <p className="mb-1.5 text-xs text-ink-muted">Configuration</p>
              <pre className="overflow-x-auto rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink">
                {JSON.stringify(viewing.config, null, 2)}
              </pre>
            </div>
            <p className="text-xs text-ink-subtle">
              Ingested content from this source appears on the Knowledge page.
            </p>

            <div>
              <p className="mb-2 text-xs text-ink-muted">Run history</p>
              {runsQuery.isLoading && <LoadingState label="Loading run history…" />}
              {runsQuery.isError && <ErrorState onRetry={() => runsQuery.refetch()} />}
              {runsQuery.data && runsQuery.data.length === 0 && (
                <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-xs text-ink-subtle">
                  No ingestion runs recorded yet for this connector.
                </p>
              )}
              {runsQuery.data && runsQuery.data.length > 0 && (
                <ul className="flex flex-col gap-2">
                  {runsQuery.data.map((run) => (
                    <li
                      key={run.id}
                      className="flex flex-col gap-1 rounded-md border border-border px-3 py-2 text-xs"
                    >
                      <div className="flex items-center justify-between">
                        <StatusBadge status={run.status} />
                        <span className="text-ink-muted">
                          {run.startedAt ? formatRelativeTime(run.startedAt) : "Not started"}
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-ink-muted">
                        <span>{run.documentsProcessed} documents processed</span>
                        {run.completedAt && <span>{formatDateTime(run.completedAt)}</span>}
                      </div>
                      <p className="text-ink-subtle">
                        {run.pagesFetched ?? 0} pages · {run.itemsDiscovered ?? 0} items ·{" "}
                        {run.itemsSkipped ?? 0} unchanged · {run.chunksEmbedded ?? 0} chunks ·{" "}
                        {run.retryCount ?? 0} retries
                      </p>
                      {(run.status === "failed" || run.status === "dead_lettered") && run.failedStage && (
                        <p className="text-critical">
                          Failed at stage: {run.failedStage}
                          {run.lastErrorType ? ` (${run.lastErrorType})` : ""}
                        </p>
                      )}
                      {run.status === "dead_lettered" && canManage && (
                        <Button
                          size="sm"
                          variant="secondary"
                          isLoading={replayMutation.isPending}
                          onClick={() => replayMutation.mutate({ connectorId: run.connectorConfigId, jobId: run.id })}
                        >
                          Replay run
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
