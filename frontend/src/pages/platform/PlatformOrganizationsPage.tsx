import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Building2, Search } from "lucide-react";
import { listPlatformOrganizations } from "@/api/platform";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { Input } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/LoadingState";
import { Select } from "@/components/ui/Select";
import { PageHeader } from "@/components/layout/PageHeader";
import { useDebounce } from "@/hooks/useDebounce";
import type { OrganizationStatus } from "@/types/platform";
import { statusLabel, statusTone } from "@/utils/platform";

const PAGE_SIZE = 25;

export function PlatformOrganizationsPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<OrganizationStatus | "">("");
  const [offset, setOffset] = useState(0);
  const debouncedSearch = useDebounce(search, 300);

  const query = useQuery({
    queryKey: ["platform", "organizations", debouncedSearch, status, offset],
    queryFn: () => listPlatformOrganizations({ search: debouncedSearch, status, limit: PAGE_SIZE, offset }),
    retry: false,
  });

  const total = query.data?.total ?? 0;

  return (
    <>
      <PageHeader title="Organizations" description="Every customer organization on this deployment." />
      <Card>
        <div className="flex flex-wrap items-end gap-3 border-b border-white/10 px-5 py-4">
          <div className="min-w-[220px] flex-1">
            <label htmlFor="org-search" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Search
            </label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-subtle" aria-hidden="true" />
              <Input
                id="org-search"
                className="pl-8"
                placeholder="Name or slug"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setOffset(0);
                }}
              />
            </div>
          </div>
          <div className="w-44">
            <label htmlFor="org-status" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Status
            </label>
            <Select
              id="org-status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as OrganizationStatus | "");
                setOffset(0);
              }}
            >
              <option value="">All statuses</option>
              <option value="onboarding">Onboarding</option>
              <option value="active">Active</option>
              <option value="suspended">Suspended</option>
            </Select>
          </div>
        </div>

        {query.isLoading ? (
          <LoadingState label="Loading organizations…" />
        ) : query.isError ? (
          <ErrorState title="Could not load organizations" onRetry={() => void query.refetch()} />
        ) : query.data && query.data.items.length === 0 ? (
          <EmptyState icon={Building2} title="No organizations match" description="Try a different search or status." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm" data-testid="organizations-table">
              <thead className="text-xs uppercase tracking-wide text-ink-subtle">
                <tr>
                  <th className="px-5 py-2.5 font-medium">Organization</th>
                  <th className="px-5 py-2.5 font-medium">Status</th>
                  <th className="px-5 py-2.5 font-medium">Members</th>
                  <th className="px-5 py-2.5 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {query.data?.items.map((org) => (
                  <tr key={org.id} className="border-t border-white/5">
                    <td className="px-5 py-3">
                      <Link to={`/platform/organizations/${org.id}`} className="font-medium text-ink hover:text-accent">
                        {org.name}
                      </Link>
                      <div className="font-mono text-xs text-ink-subtle">{org.slug}</div>
                    </td>
                    <td className="px-5 py-3">
                      <Badge tone={statusTone(org.status)}>{statusLabel(org.status)}</Badge>
                    </td>
                    <td className="px-5 py-3 text-ink-muted">{org.memberCount}</td>
                    <td className="px-5 py-3 text-ink-muted">{new Date(org.createdAt).toLocaleDateString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {query.data && total > 0 && (
          <div className="flex items-center justify-between border-t border-white/10 px-5 py-3 text-xs text-ink-muted">
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <div className="flex gap-2">
              <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Previous
              </Button>
              <Button size="sm" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>
    </>
  );
}
