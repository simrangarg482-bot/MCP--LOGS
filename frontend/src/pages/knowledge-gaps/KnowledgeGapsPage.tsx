import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lightbulb, XCircle } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { useToast } from "@/context/ToastContext";
import { dismissGapReport, listGapReports } from "@/api/knowledge";
import type { GapReport } from "@/types/knowledge";
import type { ApiError } from "@/types/common";
import { formatRelativeTime } from "@/utils/date";

const ACTION_LABEL = {
  new_runbook: "Suggests a new runbook",
  update_existing: "Suggests updating an existing document",
} as const;

/**
 * `GET /knowledge/gaps` for the list; `POST /knowledge/gaps/{id}/dismiss`
 * (`app.agents.service.dismiss_gap_report`, `knowledge:review`) for
 * dismissal -- one-way, there is no "re-open" action. `relatedDocumentId`
 * (populated when the gap agent matched an existing document rather than
 * suggesting a brand new runbook) links into the Knowledge browse page;
 * `client.ts`'s automatic snake_case<->camelCase conversion already maps
 * the backend's `related_document_id` to this field with no extra code.
 */
export function KnowledgeGapsPage() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [dismissTarget, setDismissTarget] = useState<GapReport | null>(null);

  const gapsQuery = useQuery({ queryKey: ["knowledge-gaps"], queryFn: listGapReports });

  const dismissMutation = useMutation({
    mutationFn: (id: string) => dismissGapReport(id),
    onSuccess: (dismissed) => {
      // Dismissal is one-way and this page only ever renders open gaps
      // (the "Dismissed" badge path above is otherwise unreachable here),
      // so the dismissed gap must be removed from the cached list, not
      // updated in place -- matches `KnowledgeReviewPage`'s own
      // publish/reject `.filter()` pattern for the same reason.
      queryClient.setQueryData<GapReport[]>(["knowledge-gaps"], (current) =>
        (current ?? []).filter((gap) => gap.id !== dismissed.id),
      );
      toast({ variant: "success", title: "Knowledge gap dismissed", description: dismissed.suggestedTopic });
      setDismissTarget(null);
    },
    onError: (error: ApiError) => {
      toast({
        variant: "error",
        title: "Could not dismiss knowledge gap",
        description: error.message,
      });
      setDismissTarget(null);
    },
  });

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
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge tone={gap.status === "open" ? "warning" : "neutral"}>
                      {gap.status === "open" ? "Open" : "Dismissed"}
                    </Badge>
                    {gap.status === "open" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="gap-1.5"
                        isLoading={dismissMutation.isPending && dismissTarget?.id === gap.id}
                        onClick={() => setDismissTarget(gap)}
                      >
                        <XCircle className="h-3.5 w-3.5" />
                        Dismiss
                      </Button>
                    )}
                  </div>
                </div>
                <p className="text-xs text-ink-muted">{ACTION_LABEL[gap.suggestedAction]}</p>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-subtle">
                  <span>
                    From {gap.supportingExecutionIds.length} low-confidence question
                    {gap.supportingExecutionIds.length === 1 ? "" : "s"}
                  </span>
                  <span>Flagged {formatRelativeTime(gap.createdAt)}</span>
                  {gap.relatedDocumentId && (
                    <Link to={`/knowledge/${gap.relatedDocumentId}`} className="text-accent hover:underline">
                      View related document
                    </Link>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={dismissTarget !== null}
        title="Dismiss this knowledge gap?"
        description={
          dismissTarget
            ? `"${dismissTarget.suggestedTopic}" will be marked dismissed and removed from the open list. This cannot be undone.`
            : undefined
        }
        confirmLabel="Dismiss"
        destructive
        isLoading={dismissMutation.isPending}
        onConfirm={() => dismissTarget && dismissMutation.mutate(dismissTarget.id)}
        onCancel={() => setDismissTarget(null)}
      />
    </div>
  );
}
