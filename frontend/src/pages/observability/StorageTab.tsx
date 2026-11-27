import { useQuery } from "@tanstack/react-query";
import { Database } from "lucide-react";
import { getTenantStorage } from "@/api/observability";
import { Badge } from "@/components/ui/Badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/LoadingState";
import { formatRelativeTime } from "@/utils/date";
import { formatBytes } from "@/utils/observability";

/**
 * This organization's storage footprint, from the latest DAILY snapshot (`GET /observability/storage`); the page
 * never triggers a measurement. Row counts are exact as of the snapshot. The size is an ESTIMATE and is labelled as
 * one: PostgreSQL does not attribute table size to tenants, so it is this organization's share of the chunk and
 * document tables by row count. Nothing about the whole database or its limit is shown here.
 */
export function StorageTab() {
  const query = useQuery({ queryKey: ["observability", "storage"], queryFn: () => getTenantStorage(), retry: false });

  if (query.isLoading) return <LoadingState label="Loading storage usage…" />;
  if (query.isError) return <ErrorState title="Could not load storage usage" onRetry={() => void query.refetch()} />;
  const data = query.data;
  if (!data || !data.available) {
    return (
      <EmptyState
        icon={Database}
        title="No storage snapshot yet"
        description="Storage is measured once a day. Your organization's usage will appear after the next daily snapshot."
      />
    );
  }

  return (
    <div className="flex flex-col gap-6" data-testid="storage-tab">
      <Card>
        <CardHeader>
          <CardTitle>Estimated storage used</CardTitle>
          <Badge tone="warning" data-testid="storage-estimate-badge">
            Estimate
          </Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-3xl font-semibold text-ink" data-testid="storage-estimated-bytes">
            {data.estimatedBytes === null ? "Not available" : formatBytes(data.estimatedBytes)}
          </p>
          <p className="text-xs text-ink-muted" data-testid="storage-basis">
            This is an estimate, not a measurement: PostgreSQL does not report storage per organization. It is your
            organization&apos;s share of the chunk and document tables, by row count.
            {data.takenAt && ` Snapshot taken ${formatRelativeTime(data.takenAt)}.`}
          </p>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-xs text-ink-subtle">Chunks (exact count)</dt>
              <dd className="text-ink" data-testid="storage-chunk-rows">{data.chunkRows.toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-xs text-ink-subtle">Documents (exact count)</dt>
              <dd className="text-ink" data-testid="storage-document-rows">{data.documentRows.toLocaleString()}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {data.history.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle>Growth (estimated)</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-white/10 text-sm" data-testid="storage-history">
              {[...data.history].reverse().slice(0, 14).map((point) => (
                <li key={point.takenAt} className="flex justify-between py-1.5">
                  <span className="text-ink-muted">{new Date(point.takenAt).toLocaleDateString()}</span>
                  <span className="text-ink">{point.estimatedBytes === null ? "—" : formatBytes(point.estimatedBytes)}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
