import { useQuery } from "@tanstack/react-query";
import { Lightbulb } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { listGapReports } from "@/api/knowledge";
import { formatRelativeTime } from "@/utils/date";

const ACTION_LABEL = {
  new_runbook: "Suggests a new runbook",
  update_existing: "Suggests updating an existing document",
} as const;

export function KnowledgeGapsPage() {
  const gapsQuery = useQuery({ queryKey: ["knowledge-gaps"], queryFn: listGapReports });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Knowledge gaps"
        description="Topics the Knowledge Gap Agent found recurring, low-confidence questions about -- each one is a recommendation, not an automatic action."
      />

      {gapsQuery.isLoading && <LoadingState label="Loading knowledge gaps…" />}
      {gapsQuery.isError && <ErrorState onRetry={() => gapsQuery.refetch()} />}

      {gapsQuery.data && gapsQuery.data.length === 0 && (
        <Card>
          <CardContent>
            <EmptyState
              icon={Lightbulb}
              title="No open knowledge gaps"
              description="The Knowledge Gap Agent hasn't flagged any recurring under-documented topics yet."
            />
          </CardContent>
        </Card>
      )}

      {gapsQuery.data && gapsQuery.data.length > 0 && (
        <div className="flex flex-col gap-3">
          {gapsQuery.data.map((gap) => (
            <Card key={gap.id}>
              <CardContent className="flex flex-col gap-2">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium text-ink">{gap.suggestedTopic}</p>
                  <Badge tone={gap.status === "open" ? "warning" : "neutral"}>
                    {gap.status === "open" ? "Open" : "Dismissed"}
                  </Badge>
                </div>
                <p className="text-xs text-ink-muted">{ACTION_LABEL[gap.suggestedAction]}</p>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-subtle">
                  <span>
                    From {gap.supportingExecutionIds.length} low-confidence question
                    {gap.supportingExecutionIds.length === 1 ? "" : "s"}
                  </span>
                  <span>Flagged {formatRelativeTime(gap.createdAt)}</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
