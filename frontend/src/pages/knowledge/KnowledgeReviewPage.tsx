import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle, BookOpenCheck } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { listProposedDocuments, publishDocument, rejectDocument } from "@/api/knowledge";
import type { KnowledgeDocument } from "@/types/knowledge";
import type { ApiError } from "@/types/common";
import { formatDateTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

/**
 * `GET /knowledge/proposed` (`knowledge:review`, org-level) for the list;
 * `POST /knowledge/{id}/publish` / `POST /knowledge/{id}/reject`
 * (`knowledge:review`, project-scoped) for the actions -- see
 * `app.core.knowledge.service`'s own docstrings for the exact permission
 * shape. The backend has no `"rejected"` status: a rejected document is
 * soft-deleted and simply disappears from this list on the next fetch,
 * which is why rejection here is represented as removal, not a status
 * change (matching `rejectDocument`'s own doc comment).
 */
export function KnowledgeReviewPage() {
  const pageSize = 50;
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [publishTarget, setPublishTarget] = useState<KnowledgeDocument | null>(null);
  const [rejectTarget, setRejectTarget] = useState<KnowledgeDocument | null>(null);
  const [page, setPage] = useState(0);

  // UX only -- the backend re-checks `knowledge:review` on every mutation
  // regardless of what this renders (see `publish_document`/
  // `reject_document`'s own `require_project_permission` calls).
  const canReview = Boolean(user?.permissions.includes("knowledge:review"));

  const documentsQuery = useQuery({
    queryKey: ["knowledge", "proposed", page],
    queryFn: () => listProposedDocuments(pageSize, page * pageSize),
    enabled: canReview,
  });

  const publishMutation = useMutation({
    mutationFn: (id: string) => publishDocument(id),
    onSuccess: (published) => {
      queryClient.setQueryData<KnowledgeDocument[]>(["knowledge", "proposed", page], (current) =>
        (current ?? []).filter((doc) => doc.id !== published.id),
      );
      queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      toast({ variant: "success", title: "Document published", description: published.title ?? undefined });
      setPublishTarget(null);
    },
    onError: (error: ApiError) => {
      toast({
        variant: "error",
        title: "Could not publish document",
        description: error.message,
      });
      setPublishTarget(null);
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (id: string) => rejectDocument(id),
    onSuccess: (rejected) => {
      queryClient.setQueryData<KnowledgeDocument[]>(["knowledge", "proposed", page], (current) =>
        (current ?? []).filter((doc) => doc.id !== rejected.id),
      );
      toast({ variant: "success", title: "Document rejected", description: rejected.title ?? undefined });
      setRejectTarget(null);
    },
    onError: (error: ApiError) => {
      toast({
        variant: "error",
        title: "Could not reject document",
        description: error.message,
      });
      setRejectTarget(null);
    },
  });

  if (!canReview) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader
          title="Knowledge Review"
          description="Review documents proposed by connectors and agents before they're published."
        />
        <p className="rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink-muted">
          You need the <code className="font-mono">knowledge:review</code> permission to review proposed
          documents. Contact an organization administrator if you believe this is a mistake.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Knowledge Review"
        description="Review documents proposed by connectors and agents before they're published."
      />

      {documentsQuery.isLoading && <LoadingState label="Loading proposed documents…" />}

      {documentsQuery.isError && (
        <ErrorState
          title="Could not load proposed documents"
          onRetry={() => documentsQuery.refetch()}
        />
      )}

      {documentsQuery.data && documentsQuery.data.length === 0 && (
        <EmptyState
          icon={BookOpenCheck}
          title="Nothing to review"
          description="No documents are currently proposed. Newly ingested or agent-suggested runbooks will appear here."
        />
      )}

      {documentsQuery.data && documentsQuery.data.length > 0 && (
        <div className="flex flex-col gap-3">
          {documentsQuery.data.map((doc) => (
            <Card key={doc.id}>
              <CardContent className="flex flex-col gap-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="break-words text-sm font-semibold text-ink">{doc.title ?? "(untitled)"}</h3>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <Badge tone="neutral">{titleCase(doc.source)}</Badge>
                      <Badge tone="warning">Proposed</Badge>
                      <Badge tone="neutral">v{doc.version}</Badge>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button
                      size="sm"
                      variant="danger"
                      className="gap-1.5"
                      isLoading={rejectMutation.isPending && rejectTarget?.id === doc.id}
                      onClick={() => setRejectTarget(doc)}
                    >
                      <XCircle className="h-3.5 w-3.5" />
                      Reject
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      className="gap-1.5"
                      isLoading={publishMutation.isPending && publishTarget?.id === doc.id}
                      onClick={() => setPublishTarget(doc)}
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      Publish
                    </Button>
                  </div>
                </div>

                <p className="whitespace-pre-line text-sm leading-relaxed text-ink-muted">
                  {doc.content ?? "No preview content is available for this document."}
                </p>

                <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-subtle">
                  <span>Created: {formatDateTime(doc.createdAt)}</span>
                  <span>Updated: {formatDateTime(doc.updatedAt)}</span>
                  {doc.sourceIncidentId && <span>From incident: {doc.sourceIncidentId}</span>}
                </div>
              </CardContent>
            </Card>
          ))}
          <div className="flex items-center justify-between border-t border-border pt-3">
            <Button
              size="sm"
              variant="secondary"
              disabled={page === 0}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Previous
            </Button>
            <span className="text-xs text-ink-muted">Page {page + 1}</span>
            <Button
              size="sm"
              variant="secondary"
              disabled={documentsQuery.data.length < pageSize}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={publishTarget !== null}
        title="Publish this document?"
        description={
          publishTarget
            ? `"${publishTarget.title ?? "(untitled)"}" will become visible to everyone in your organization and searchable from Ask/Search.`
            : undefined
        }
        confirmLabel="Publish"
        isLoading={publishMutation.isPending}
        onConfirm={() => publishTarget && publishMutation.mutate(publishTarget.id)}
        onCancel={() => setPublishTarget(null)}
      />

      <ConfirmDialog
        open={rejectTarget !== null}
        title="Reject this document?"
        description={
          rejectTarget
            ? `"${rejectTarget.title ?? "(untitled)"}" will be permanently removed from the review queue. This cannot be undone.`
            : undefined
        }
        confirmLabel="Reject"
        destructive
        isLoading={rejectMutation.isPending}
        onConfirm={() => rejectTarget && rejectMutation.mutate(rejectTarget.id)}
        onCancel={() => setRejectTarget(null)}
      />
    </div>
  );
}
