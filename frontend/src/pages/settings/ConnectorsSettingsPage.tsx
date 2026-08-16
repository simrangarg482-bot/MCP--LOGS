import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/data/StatusBadge";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { listConnectors } from "@/api/connectors";
import type { Connector } from "@/types/connector";
import { formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

// A read-only summary; connecting, syncing, and viewing per-connector detail
// happens on the full Connectors page (/connectors) -- this settings tab
// links there rather than duplicating that interactive UI.
export function ConnectorsSettingsPage() {
  const connectorsQuery = useQuery({ queryKey: ["connectors"], queryFn: listConnectors });

  const columns: DataTableColumn<Connector>[] = [
    {
      key: "source",
      header: "Connector",
      render: (row) => <span className="font-medium text-ink">{titleCase(row.source)}</span>,
    },
    { key: "status", header: "Status", render: (row) => <StatusBadge status={row.status} /> },
    {
      key: "lastSyncedAt",
      header: "Last sync",
      render: (row) => (row.lastSyncedAt ? formatRelativeTime(row.lastSyncedAt) : "Never"),
    },
  ];

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold text-ink">Connectors</h3>
        <Link to="/connectors" className="text-xs font-medium text-accent hover:underline">
          Manage connectors →
        </Link>
      </div>
      <DataTable
        columns={columns}
        rows={connectorsQuery.data ?? []}
        rowKey={(row) => row.id}
        isLoading={connectorsQuery.isLoading}
        isError={connectorsQuery.isError}
        onRetry={() => connectorsQuery.refetch()}
      />
    </Card>
  );
}
