import type { IncidentStatus } from "@/types/incident";
import type { ConnectorStatus } from "@/types/connector";
import type { DocumentStatus } from "@/types/knowledge";
import type { IngestionRunStatus } from "@/types/ingestion";
import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { titleCase } from "@/utils/format";

type AnyStatus = IncidentStatus | ConnectorStatus | DocumentStatus | IngestionRunStatus;

const STATUS_TONE: Record<AnyStatus, BadgeTone> = {
  open: "warning",
  investigating: "accent",
  resolved: "success",
  closed: "neutral",
  active: "success",
  connecting: "accent",
  disconnected: "neutral",
  error: "critical",
  published: "success",
  proposed: "warning",
  queued: "neutral",
  running: "accent",
  succeeded: "success",
  failed: "critical",
};

export function StatusBadge({ status }: { status: AnyStatus }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{titleCase(status)}</Badge>;
}
