import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plug, Plus } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { ConnectorCard } from "@/components/domain/ConnectorCard";
import { ConnectConnectorModal } from "@/components/domain/ConnectConnectorModal";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { Drawer } from "@/components/ui/Drawer";
import { StatusBadge } from "@/components/data/StatusBadge";
import {
  createGithubConnector,
  createSlackConnector,
  listConnectors,
  triggerConnectorSync,
} from "@/api/connectors";
import { listIngestionRuns } from "@/api/ingestion";
import type { Connector } from "@/types/connector";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatDateTime, formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

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

  const connectorsQuery = useQuery({ queryKey: ["connectors"], queryFn: listConnectors });

  const runsQuery = useQuery({
    queryKey: ["ingestion-runs", viewing?.id],
    queryFn: () => listIngestionRuns(viewing!.id),
    enabled: Boolean(viewing),
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

      {connectorsQuery.isLoading && <LoadingState label="Loading connectors…" />}
      {connectorsQuery.isError && <ErrorState onRetry={() => connectorsQuery.refetch()} />}
      {connectorsQuery.data && connectorsQuery.data.length === 0 && (
        <EmptyState
          icon={Plug}
          title="No connectors configured"
          description="Connect GitHub or Slack to start ingesting data EKIP can answer questions about."
          action={
            canManage ? (
              <Button variant="primary" className="gap-1.5" onClick={() => setIsConnectOpen(true)}>
                <Plus className="h-4 w-4" />
                Connect a source
              </Button>
            ) : undefined
          }
        />
      )}

      {connectorsQuery.data && connectorsQuery.data.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {connectorsQuery.data.map((connector) => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              onView={setViewing}
              onSync={(c) => syncMutation.mutate(c)}
              isSyncing={syncMutation.isPending && syncMutation.variables?.id === connector.id}
              canManage={canManage}
            />
          ))}
        </div>
      )}

      <ConnectConnectorModal
        open={isConnectOpen}
        onClose={() => setIsConnectOpen(false)}
        isSubmitting={githubMutation.isPending || slackMutation.isPending}
        onSubmitGithub={async (token, repos) => {
          await githubMutation.mutateAsync({ token, repos });
        }}
        onSubmitSlack={async (token, channelIds) => {
          await slackMutation.mutateAsync({ token, channelIds });
        }}
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
                      {run.status === "failed" && run.failedStage && (
                        <p className="text-critical">Failed at stage: {run.failedStage}</p>
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
