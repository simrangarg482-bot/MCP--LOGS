import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { FileCode, FileText, MessageSquare, Search as SearchIcon } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { SearchBar } from "@/components/data/SearchBar";
import { Badge } from "@/components/ui/Badge";
import { LoadingState } from "@/components/ui/LoadingState";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { useDebounce } from "@/hooks/useDebounce";
import { globalSearch } from "@/api/search";
import type { ScoredChunk } from "@/types/ask";
import { formatPercent } from "@/utils/format";

// Real `CollectionName` values (`app.retrieval.schemas.CollectionName`) --
// the only real grouping dimension `ScoredChunk` carries. The previous
// page grouped by an invented "incident/knowledge/slack/github" taxonomy
// that no real search response has ever returned.
const COLLECTION_META: Record<ScoredChunk["collection"], { label: string; icon: LucideIcon }> = {
  documentation: { label: "Documentation", icon: FileText },
  code: { label: "Code", icon: FileCode },
  conversations: { label: "Conversations", icon: MessageSquare },
};

const COLLECTION_ORDER: ScoredChunk["collection"][] = ["documentation", "code", "conversations"];

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  const debouncedQuery = useDebounce(query, 300);

  const searchQuery = useQuery({
    queryKey: ["search", debouncedQuery],
    queryFn: () => globalSearch(debouncedQuery),
    enabled: debouncedQuery.trim().length > 0,
  });

  const grouped = useMemo(() => {
    const results = searchQuery.data ?? [];
    const groups: Record<ScoredChunk["collection"], ScoredChunk[]> = {
      documentation: [],
      code: [],
      conversations: [],
    };
    for (const chunk of results) groups[chunk.collection].push(chunk);
    return groups;
  }, [searchQuery.data]);

  function handleChange(value: string) {
    setQuery(value);
    setParams(value ? { q: value } : {});
  }

  const hasQuery = debouncedQuery.trim().length > 0;
  const totalResults = searchQuery.data?.length ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Search"
        description="Searches the knowledge base (documentation, code, and conversations already ingested)."
      />

      <SearchBar
        value={query}
        onChange={handleChange}
        placeholder="Search EKIP…"
        autoFocus
        className="max-w-2xl"
      />

      {!hasQuery && (
        <EmptyState
          icon={SearchIcon}
          title="Search across your engineering environment"
          description="Try an error message, service name, or a question."
        />
      )}

      {hasQuery && searchQuery.isLoading && <LoadingState label="Searching…" />}

      {hasQuery && searchQuery.isError && (
        <ErrorState title="Search failed" onRetry={() => searchQuery.refetch()} />
      )}

      {hasQuery && !searchQuery.isLoading && !searchQuery.isError && totalResults === 0 && (
        <EmptyState title="No results found" description="Try a different search term." />
      )}

      {hasQuery && !searchQuery.isLoading && !searchQuery.isError && totalResults > 0 && (
        <div className="flex flex-col gap-6">
          {COLLECTION_ORDER.filter((collection) => grouped[collection].length > 0).map((collection) => {
            const { label, icon: Icon } = COLLECTION_META[collection];
            return (
              <section key={collection}>
                <div className="mb-2 flex items-center gap-2 border-b border-border pb-2">
                  <Icon className="h-4 w-4 text-ink-muted" />
                  <h2 className="text-sm font-semibold text-ink">{label}</h2>
                  <Badge tone="neutral">{grouped[collection].length}</Badge>
                </div>
                <ul className="flex flex-col gap-1">
                  {grouped[collection].map((chunk) => (
                    <SearchResultRow key={chunk.chunkId} chunk={chunk} />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SearchResultRow({ chunk }: { chunk: ScoredChunk }) {
  return (
    <li>
      <a
        href={chunk.sourceUrl ?? "#"}
        target={chunk.sourceUrl ? "_blank" : undefined}
        rel="noreferrer"
        className="flex flex-col gap-1 rounded-md px-3 py-2.5 hover:bg-slate-50"
      >
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm font-medium text-ink">{chunk.title ?? "Untitled"}</p>
          <span className="shrink-0 text-xs text-ink-subtle">{formatPercent(chunk.score)} match</span>
        </div>
        <p className="line-clamp-2 text-sm text-ink-muted">{chunk.content}</p>
      </a>
    </li>
  );
}
