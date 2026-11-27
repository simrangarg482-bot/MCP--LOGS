import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import { getIngestionQueueHealth, listIngestionStats } from "@/api/observability";
import { listConnectors } from "@/api/connectors";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/LoadingState";
import { useAuth } from "@/context/AuthContext";
import { formatDurationMs } from "@/utils/format";
import { formatAge, sinceFor, type TimeWindow } from "@/utils/observability";
import type { ApiError } from "@/types/common";
import type { IngestionJobStats } from "@/types/ingestion";

function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

/**
 * Ingestion monitoring from the two real endpoints and nothing else:
 *  - `GET /observability/ingestion` (per-connector run aggregates, optional `since`)
 *  - `GET /observability/ingestion/queue` (live queue: jobs waiting, oldest wait, worker concurrency)
 *
 * What the backend does NOT expose, and this tab therefore does not show: per-status counts of queued or
 * running jobs in the database, a list of failed jobs, the connector's name in the stats rows, or the
 * number of jobs currently running. No "health score" is computed -- only the numbers the API returns.
 */
export function IngestionTab({ window }: { window: TimeWindow }) {
  const { user } = useAuth();
  const statsQuery = useQuery({
    queryKey: ["observability", "ingestion", "stats", window],
    queryFn: () => listIngestionStats(sinceFor(window)),
  });
  const queueQuery = useQuery({
    queryKey: ["observability", "ingestion", "queue"],
    queryFn: () => getIngestionQueueHealth(),
    retry: false,
  });

  // Connector names come from the connectors list, which needs `tenancy:manage`. Without it we show the
  // connector id only -- the stats endpoint does not carry a name.
  const canListConnectors = Boolean(user?.permissions.includes("tenancy:manage"));
  const connectorsQuery = useQuery({
    queryKey: ["observability", "ingestion", "connectors"],
    queryFn: () => listConnectors(),
    enabled: canListConnectors,
    retry: false,
  });
  const connectorLabel = (row: IngestionJobStats): string => {
    const connector = connectorsQuery.data?.find((c) => c.id === row.connectorConfigId);
    return connector ? `${connector.source} · ${shortId(connector.id)}` : shortId(row.connectorConfigId);
  };

  const rows = statsQuery.data ?? [];
  const queueError = queueQuery.error as ApiError | null;
  const queue = queueQuery.data;

  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="ingestion-queue-heading" className="flex flex-col gap-3">
        <h2 id="ingestion-queue-heading" className="text-sm font-semibold text-ink">
          Job queue
        </h2>
        {queueQuery.isLoading && <LoadingState label="Checking the job queue…" />}
        {queueQuery.isError && (
          <div
            role="alert"
            className="rounded-md border border-border bg-white/[0.03] px-3 py-3 text-xs text-ink-muted"
            data-testid="queue-unavailable"
          >
            <p className="font-medium text-ink">
              {queueError?.status === 503 ? "Queue status is unavailable" : "Could not read the job queue"}
            </p>
            <p className="mt-1">
              {queueError?.status === 503
                ? "The background job queue cannot be reached right now, so waiting jobs cannot be counted. Run history below does not depend on it."
                : (queueError?.message ?? "The request failed.")}
            </p>
            <button
              type="button"
              className="mt-2 font-medium text-accent hover:underline"
              onClick={() => void queueQuery.refetch()}
            >
              Retry
            </button>
          </div>
        )}
        {queue && (
          <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3" data-testid="queue-health">
            <Metric label="Jobs waiting in the queue" value={queue.queuedJobs.toLocaleString()} testId="queue-depth" />
            <Metric
              label="Oldest waiting job"
              value={queue.oldestQueuedAgeSeconds === null ? "None waiting" : formatAge(queue.oldestQueuedAgeSeconds)}
              testId="queue-oldest"
            />
            <Metric
              label="Worker max concurrent jobs"
              value={queue.workerMaxConcurrency.toLocaleString()}
              testId="queue-concurrency"
            />
          </dl>
        )}
        {queue && (
          <p className="text-xs text-ink-subtle">
            Jobs waiting to start, read live from the queue. It does not include jobs that are already running.
          </p>
        )}
      </section>

      <section aria-labelledby="ingestion-runs-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="ingestion-runs-heading" className="text-sm font-semibold text-ink">
            Ingestion runs by connector
          </h2>
          {canListConnectors && (
            <Link to="/connectors" className="text-xs font-medium text-accent hover:underline">
              Run history per connector
            </Link>
          )}
        </div>

        {statsQuery.isLoading && <LoadingState label="Loading ingestion statistics…" />}
        {statsQuery.isError && (
          <ErrorState title="Could not load ingestion statistics" onRetry={() => void statsQuery.refetch()} />
        )}
        {statsQuery.data && rows.length === 0 && (
          <EmptyState
            icon={Layers}
            title="No ingestion runs recorded yet"
            description={
              window === "all"
                ? "Statistics appear once a connector has synced for your organization."
                : "No ingestion runs in this time window. Try a longer window."
            }
          />
        )}
        {rows.length > 0 && (
          <div className="flex flex-col gap-3" data-testid="ingestion-stats">
            {rows.map((row) => (
              <Card key={row.connectorConfigId} data-testid={`ingestion-connector-${row.connectorConfigId}`}>
                <CardHeader>
                  <CardTitle>
                    <code className="text-sm">{connectorLabel(row)}</code>
                  </CardTitle>
                  <span className="text-xs text-ink-muted">{row.runCount.toLocaleString()} runs</span>
                </CardHeader>
                <CardContent>
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-xs sm:grid-cols-4">
                    <Stat label="Succeeded" value={row.succeededCount} />
                    <Stat label="Failed" value={row.failedCount} critical={row.failedCount > 0} />
                    <Stat label="Dead-lettered" value={row.deadLetteredCount} critical={row.deadLetteredCount > 0} />
                    <Stat label="Retries" value={row.totalRetries} />
                    <Stat
                      label="Avg duration (successful runs)"
                      value={row.avgDurationSeconds === null ? "—" : formatDurationMs(row.avgDurationSeconds * 1000)}
                    />
                    <Stat label="Documents processed" value={row.totalDocumentsProcessed} />
                    <Stat label="Pages fetched" value={row.totalPagesFetched} />
                    <Stat label="Chunks embedded" value={row.totalChunksEmbedded} />
                    <Stat label="Items discovered" value={row.totalItemsDiscovered} />
                    <Stat label="Items skipped" value={row.totalItemsSkipped} />
                  </dl>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
        {rows.length > 0 && !canListConnectors && (
          <p className="text-xs text-ink-subtle">
            Connectors are identified by id: connector names are only shown to people who can manage connectors.
          </p>
        )}
      </section>
    </div>
  );
}

function Metric({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3 shadow-subtle">
      <dt className="text-xs text-ink-subtle">{label}</dt>
      <dd className="mt-1 text-lg font-semibold text-ink" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

function Stat({ label, value, critical }: { label: string; value: number | string; critical?: boolean }) {
  return (
    <div>
      <dt className="text-ink-subtle">{label}</dt>
      <dd className={critical ? "font-medium text-critical" : "font-medium text-ink"}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </dd>
    </div>
  );
}
