import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  DocumentProposalRequest,
  DocumentUpdateRequest,
  GapReport,
  KnowledgeDocument,
  KnowledgeFilters,
} from "@/types/knowledge";
import { mockGapReports, mockKnowledgeDocuments } from "@/mocks/data/knowledge";

/**
 * `GET /knowledge` -- server-side `limit`/`offset` pagination (Stage 3),
 * one `source` value filtered server-side. There is no server-side
 * search-text parameter and no total count, so:
 *  - `filters.search` only narrows the page already fetched, not the full
 *    dataset (a page can look "empty of matches" even when a later page
 *    has some -- this is an accepted tradeoff of true server-side paging,
 *    the same shape `listProposedDocuments` below already has).
 *  - Callers get a plain array back (no `Paginated<T>` wrapper, since
 *    there is no `total` to put in it) and drive "next page" off
 *    `results.length < pageSize`, mirroring `KnowledgeReviewPage`'s
 *    existing Previous/Next pattern for `listProposedDocuments`.
 */
export async function listKnowledgeDocuments(
  filters: KnowledgeFilters = {},
): Promise<KnowledgeDocument[]> {
  const pageSize = filters.pageSize ?? 20;
  const page = filters.page ?? 1;
  const offset = (page - 1) * pageSize;

  if (USE_MOCK_DATA) {
    // Mirrors the real endpoint: browse-page mocks are published documents
    // only -- a "proposed" mock (manual, pending review) belongs in
    // `listProposedDocuments` instead, never here.
    let result = mockKnowledgeDocuments.filter((d) => d.status === "published");
    if (filters.search) {
      const q = filters.search.toLowerCase();
      result = result.filter((d) => (d.title ?? "").toLowerCase().includes(q));
    }
    if (filters.source?.length) {
      result = result.filter((d) => filters.source!.includes(d.source));
    }
    result = [...result].sort((a, b) => (a.updatedAt > b.updatedAt ? -1 : 1));
    return mockDelay(result.slice(offset, offset + pageSize));
  }

  const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
  if (filters.source?.length) params.set("source", filters.source[0]);

  let documents = await apiRequest<KnowledgeDocument[]>(`/knowledge?${params.toString()}`);
  if (filters.search) {
    const q = filters.search.toLowerCase();
    documents = documents.filter((d) => (d.title ?? "").toLowerCase().includes(q));
  }
  return documents;
}

/**
 * `POST /knowledge` (`app.core.knowledge.service.propose_document`, no
 * `knowledge:review` gate -- any authenticated org member may propose a
 * document; see `propose_document`'s own docstring in
 * `app/api/routers/knowledge.py`). Always creates `source="manual"`,
 * `status="proposed"` -- identical to the mock branch below and to what
 * `listProposedDocuments`/`KnowledgeReviewPage` already expect to see.
 */
export async function proposeDocument(data: DocumentProposalRequest): Promise<KnowledgeDocument> {
  if (USE_MOCK_DATA) {
    const now = new Date().toISOString();
    const doc: KnowledgeDocument = {
      id: crypto.randomUUID(),
      // Matches `MOCK_ORG_ID`/`MOCK_PROJECT_ID` in `mocks/data/knowledge.ts`
      // (not exported, so duplicated here rather than widening that
      // module's public surface for one caller).
      organizationId: "org-1",
      projectId: data.projectId ?? "project-1",
      title: data.title,
      status: "proposed",
      version: 1,
      content: data.content,
      source: "manual",
      sourceUrl: null,
      sourceIncidentId: data.sourceIncidentId ?? null,
      createdAt: now,
      updatedAt: now,
    };
    mockKnowledgeDocuments.push(doc);
    return mockDelay(doc);
  }
  return apiRequest<KnowledgeDocument>(`/knowledge`, { method: "POST", body: data });
}

export async function getKnowledgeDocument(id: string): Promise<KnowledgeDocument> {
  if (USE_MOCK_DATA) {
    const doc = mockKnowledgeDocuments.find((d) => d.id === id);
    if (!doc) throw { status: 404, message: "Document not found" };
    return mockDelay(doc);
  }
  return apiRequest<KnowledgeDocument>(`/knowledge/${id}`);
}

export async function listProposedDocuments(
  limit = 50,
  offset = 0,
): Promise<KnowledgeDocument[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockKnowledgeDocuments.filter((d) => d.status === "proposed"));
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiRequest<KnowledgeDocument[]>(`/knowledge/proposed?${params.toString()}`);
}

/**
 * `POST /knowledge/{id}/publish` (`app.core.knowledge.service.publish_document`,
 * permission `knowledge:review`, project-scoped). Only valid from
 * `status="proposed"` -- the backend returns a 409 (`document.not_proposed`)
 * for an already-published document, which this function does not attempt
 * to pre-check client-side (the backend is authoritative).
 */
export async function publishDocument(id: string): Promise<KnowledgeDocument> {
  if (USE_MOCK_DATA) {
    const doc = mockKnowledgeDocuments.find((d) => d.id === id);
    if (!doc) throw { status: 404, message: "Document not found" };
    doc.status = "published";
    doc.updatedAt = new Date().toISOString();
    return mockDelay(doc);
  }
  return apiRequest<KnowledgeDocument>(`/knowledge/${id}/publish`, { method: "POST" });
}

/**
 * `POST /knowledge/{id}/reject` (`app.core.knowledge.service.reject_document`).
 * Soft-deletes the document -- the backend's own returned `status` stays
 * `"proposed"` (there is no `"rejected"` status value anywhere in the real
 * schema), and the document becomes invisible (404) on any subsequent
 * fetch. Callers should treat a successful call as "this document is gone
 * from the review queue," not toggle a `"rejected"` badge that doesn't
 * exist server-side.
 */
export async function rejectDocument(id: string): Promise<KnowledgeDocument> {
  if (USE_MOCK_DATA) {
    const index = mockKnowledgeDocuments.findIndex((d) => d.id === id);
    if (index === -1) throw { status: 404, message: "Document not found" };
    const [doc] = mockKnowledgeDocuments.splice(index, 1);
    return mockDelay(doc);
  }
  return apiRequest<KnowledgeDocument>(`/knowledge/${id}/reject`, { method: "POST" });
}

/**
 * `PATCH /knowledge/{id}` (`app.core.knowledge.service.update_document`).
 * `exclude_unset` semantics on the backend -- only send fields that
 * actually changed; omitted fields are left untouched, not cleared.
 */
export async function updateDocument(
  id: string,
  data: DocumentUpdateRequest,
): Promise<KnowledgeDocument> {
  if (USE_MOCK_DATA) {
    const doc = mockKnowledgeDocuments.find((d) => d.id === id);
    if (!doc) throw { status: 404, message: "Document not found" };
    if (data.title !== undefined) doc.title = data.title;
    if (data.content !== undefined) {
      doc.content = data.content;
      doc.version += 1;
    }
    doc.updatedAt = new Date().toISOString();
    return mockDelay(doc);
  }
  return apiRequest<KnowledgeDocument>(`/knowledge/${id}`, { method: "PATCH", body: data });
}

export async function listGapReports(): Promise<GapReport[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockGapReports);
  }
  return apiRequest<GapReport[]>(`/knowledge/gaps`);
}

/**
 * `POST /knowledge/gaps/{id}/dismiss` (`app.agents.service.dismiss_gap_report`,
 * permission `knowledge:review`). One-way -- there is no endpoint to
 * un-dismiss a gap report.
 */
export async function dismissGapReport(id: string): Promise<GapReport> {
  if (USE_MOCK_DATA) {
    const gap = mockGapReports.find((g) => g.id === id);
    if (!gap) throw { status: 404, message: "Gap report not found" };
    gap.status = "dismissed";
    gap.updatedAt = new Date().toISOString();
    return mockDelay(gap);
  }
  return apiRequest<GapReport>(`/knowledge/gaps/${id}/dismiss`, { method: "POST" });
}
