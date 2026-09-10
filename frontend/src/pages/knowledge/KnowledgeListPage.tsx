import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { SearchBar } from "@/components/data/SearchBar";
import { FilterBar } from "@/components/data/FilterBar";
import { Select } from "@/components/ui/Select";
import { Card } from "@/components/ui/Card";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { useDebounce } from "@/hooks/useDebounce";
import { useToast } from "@/context/ToastContext";
import { listKnowledgeDocuments, proposeDocument } from "@/api/knowledge";
import type { KnowledgeDocument, KnowledgeSource } from "@/types/knowledge";
import type { ApiError } from "@/types/common";
import { formatDateTime, formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

// Mirrors `app.core.tenancy.schemas.ConnectorSource` plus `"manual"` --
// same list `KnowledgeSource` (frontend/src/types/knowledge.ts) declares.
const SOURCES: KnowledgeSource[] = [
  "github",
  "slack",
  "manual",
  "teams",
  "azure_devops",
  "jira",
  "confluence",
  "sharepoint",
  "runbooks",
  "google_drive",
  "gitlab",
  "notion",
  "servicenow",
  "pagerduty",
  "monitoring",
  "incidents",
];

export function KnowledgeListPage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [source, setSource] = useState<KnowledgeSource | "">("");
  const [page, setPage] = useState(0);
  const pageSize = 10;

  const [submitOpen, setSubmitOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newContent, setNewContent] = useState("");

  const debouncedSearch = useDebounce(search, 300);

  // `page` here is 0-indexed and maps to `GET /knowledge`'s `offset`
  // (via `listKnowledgeDocuments`'s `page`/`pageSize` -> `limit`/`offset`
  // translation) -- same server-side pagination shape `KnowledgeReviewPage`
  // already uses for `listProposedDocuments`. Note the backend has no
  // search-text parameter, so `search` only narrows the page already
  // fetched, not the full document set.
  const filters = useMemo(
    () => ({
      search: debouncedSearch || undefined,
      source: source ? [source] : undefined,
      page: page + 1,
      pageSize,
    }),
    [debouncedSearch, source, page],
  );

  const documentsQuery = useQuery({
    queryKey: ["knowledge", filters],
    queryFn: () => listKnowledgeDocuments(filters),
  });

  // `POST /knowledge` (P3) -- always lands as `status="proposed"`, so it
  // never shows up in this page's published-only list; it goes to
  // Knowledge Review instead (same place connector/agent proposals land).
  const proposeMutation = useMutation({
    mutationFn: () => proposeDocument({ title: newTitle, content: newContent }),
    onSuccess: () => {
      toast({
        variant: "success",
        title: "Document submitted",
        description: "It's now awaiting review in Knowledge Review before it's published.",
      });
      queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      setSubmitOpen(false);
      setNewTitle("");
      setNewContent("");
    },
    onError: (error: ApiError) => {
      toast({ variant: "error", title: "Could not submit document", description: error.message });
    },
  });

  const activeFilterCount = source ? 1 : 0;

  const columns: DataTableColumn<KnowledgeDocument>[] = [
    {
      key: "title",
      header: "Document",
      render: (row) => <span className="font-medium text-ink">{row.title ?? "(untitled)"}</span>,
    },
    {
      key: "source",
      header: "Source",
      render: (row) => <Badge tone="neutral">{titleCase(row.source)}</Badge>,
    },
    {
      key: "status",
      header: "Status",
      render: (row) => <Badge tone={row.status === "published" ? "success" : "warning"}>{titleCase(row.status)}</Badge>,
    },
    {
      key: "updatedAt",
      header: "Updated",
      render: (row) => (
        <span title={formatDateTime(row.updatedAt)} className="text-ink-muted">
          {formatRelativeTime(row.updatedAt)}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Knowledge Base"
        description="Browse knowledge ingested from connected sources -- GitHub, Slack, and manually reviewed runbooks."
        actions={
          <Button size="sm" variant="primary" className="gap-1.5" onClick={() => setSubmitOpen(true)}>
            <Plus className="h-3.5 w-3.5" />
            Submit document
          </Button>
        }
      />

      <div className="flex flex-col gap-3">
        <SearchBar
          value={search}
          onChange={(value) => {
            setSearch(value);
            setPage(0);
          }}
          placeholder="Search documents…"
          className="max-w-sm"
        />

        <FilterBar
          activeCount={activeFilterCount}
          onClear={() => {
            setSource("");
            setPage(0);
          }}
        >
          <Select
            aria-label="Filter by source"
            value={source}
            onChange={(e) => {
              setSource(e.target.value as KnowledgeSource | "");
              setPage(0);
            }}
            className="w-40"
          >
            <option value="">All sources</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {titleCase(s)}
              </option>
            ))}
          </Select>
        </FilterBar>
      </div>

      <Card>
        <DataTable
          columns={columns}
          rows={documentsQuery.data ?? []}
          rowKey={(row) => row.id}
          isLoading={documentsQuery.isLoading}
          isError={documentsQuery.isError}
          onRetry={() => documentsQuery.refetch()}
          onRowClick={(row) => navigate(`/knowledge/${row.id}`)}
          emptyTitle="No documents found"
          emptyDescription="Connect a source and sync it, or try a different search term."
        />
        <div className="flex items-center justify-between border-t border-border px-4 py-3">
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
            disabled={(documentsQuery.data?.length ?? 0) < pageSize}
            onClick={() => setPage((current) => current + 1)}
          >
            Next
          </Button>
        </div>
      </Card>

      <Modal
        open={submitOpen}
        onClose={() => setSubmitOpen(false)}
        title="Submit a document"
        description="Propose a runbook or knowledge document for your organization. It will be reviewed before it's published."
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            proposeMutation.mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
            Title
            <Input
              required
              placeholder="e.g. Database Failover Runbook"
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
            Content
            <textarea
              required
              rows={6}
              placeholder="Steps, context, or guidance future investigations should be able to find..."
              value={newContent}
              onChange={(event) => setNewContent(event.target.value)}
              className="w-full rounded-md border border-border bg-white/[0.04] px-3 py-2 text-sm text-ink placeholder:text-ink-subtle focus-visible:border-accent"
            />
          </label>

          <div className="mt-2 flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={() => setSubmitOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={proposeMutation.isPending || !newTitle || !newContent}
            >
              {proposeMutation.isPending ? "Submitting…" : "Submit for review"}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
