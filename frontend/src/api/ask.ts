import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { AskResponse, QuestionHistoryEntry, ScoredChunk } from "@/types/ask";

const MOCK_RESPONSE: AskResponse = {
  confidence: 0.86,
  routeTaken: "answer",
  answer:
    "The payments service moved to the new promo-code engine in v2.14.0, which replaced the " +
    "per-request discount lookup with a cached ruleset [1]. The rollout also introduced a config " +
    "migration that back-fills discount rows on deploy [2]. Set VITE_USE_MOCK_DATA=false to query " +
    "the real EKIP retrieval pipeline.",
  answerMode: "answered",
  citations: [
    {
      documentId: "00000000-0000-0000-0000-000000000001",
      chunkId: "00000000-0000-0000-0000-000000000002",
      sourceUrl: "https://github.com/acme/payments/blob/main/docs/promo-code-engine.md",
      excerpt:
        "v2.14.0 replaces the per-request discount lookup with a cached ruleset evaluated at " +
        "checkout. The cache is warmed on deploy and invalidated on ruleset change.",
    },
    {
      documentId: "00000000-0000-0000-0000-000000000003",
      chunkId: "00000000-0000-0000-0000-000000000004",
      sourceUrl: null,
      excerpt:
        "#deploys — promo-code migration runs as a post-deploy job; back-fills any discount rows " +
        "missing from the previous schema.",
    },
  ],
  investigation: null,
};

export async function askQuestion(query: string, incidentId?: string): Promise<AskResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_RESPONSE, 800);
  }
  return apiRequest<AskResponse>("/ask", {
    method: "POST",
    body: { query, incidentId: incidentId ?? null },
  });
}

const MOCK_INVESTIGATION_RESPONSE: AskResponse = {
  confidence: 0.58,
  routeTaken: "investigation",
  answer: null,
  answerMode: null,
  citations: [],
  investigation: {
    evidence: [
      {
        source: "commit",
        reference: "payment-service@a1b2c3d",
        summary: "Deployed payment-service v2.14.0, introducing the promo-code refactor.",
        retrievedAt: new Date().toISOString(),
        sourceTimestamp: null,
        metadata: {},
      },
    ],
    hypotheses: [
      {
        description: "Null discount configuration object introduced by the promo-code refactor.",
        confidence: 0.72,
        supportingEvidenceIds: ["payment-service@a1b2c3d"],
      },
    ],
    suggestedOwnerTeam: "Payments",
    suggestedNextSteps: [
      "Review the promo-code refactor's config migration for missing rows.",
      "Confirm the deploy timestamp against the error-rate spike.",
    ],
  },
};

export async function investigateIncident(incidentId: string): Promise<AskResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_INVESTIGATION_RESPONSE, 800);
  }
  return apiRequest<AskResponse>(`/incidents/${incidentId}/investigate`, { method: "POST" });
}

export async function getQuestionHistory(limit = 20, offset = 0): Promise<QuestionHistoryEntry[]> {
  if (USE_MOCK_DATA) {
    return mockDelay([], 300);
  }
  return apiRequest<QuestionHistoryEntry[]>(`/ask/history?limit=${limit}&offset=${offset}`);
}

const MOCK_SCORED_CHUNKS: ScoredChunk[] = [
  {
    chunkId: "00000000-0000-0000-0000-000000000010",
    documentId: "00000000-0000-0000-0000-000000000011",
    collection: "conversations",
    content: "The embedding service was consuming more memory than expected. Reduced worker concurrency from 10 to 4.",
    score: 0.78,
    sourceOffsetStart: 0,
    sourceOffsetEnd: 96,
    title: "#incidents — embedding worker memory",
    sourceUrl: null,
    metadata: {},
  },
  {
    chunkId: "00000000-0000-0000-0000-000000000012",
    documentId: "00000000-0000-0000-0000-000000000013",
    collection: "documentation",
    content: "If checkout returns 500, check the payment adapter for null handling.",
    score: 0.71,
    sourceOffsetStart: 0,
    sourceOffsetEnd: 70,
    title: "Checkout 500 runbook",
    sourceUrl: null,
    metadata: {},
  },
];

export async function searchSimilarIncidents(description: string, topK = 10): Promise<ScoredChunk[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_SCORED_CHUNKS.slice(0, topK), 500);
  }
  return apiRequest<ScoredChunk[]>("/search/similar-incidents", {
    method: "POST",
    body: { description, topK },
  });
}

export async function searchRecentChanges(
  query: string,
  options: { since?: string; topK?: number; collection?: "documentation" | "code" | "conversations" } = {},
): Promise<ScoredChunk[]> {
  if (USE_MOCK_DATA) {
    return mockDelay([], 500);
  }
  return apiRequest<ScoredChunk[]>("/search/recent-changes", {
    method: "POST",
    body: {
      query,
      since: options.since ?? null,
      topK: options.topK ?? 10,
      // `null` (not a literal collection) when the caller doesn't name
      // one -- matches `RecentChangesRequest.collection`'s own `None`
      // default on the backend, which searches "documentation" *and*
      // "code" together (see `agents_service.search_recent_changes`'s
      // docstring). A hardcoded fallback here would silently override
      // that and restrict every omitted-collection call back to one.
      collection: options.collection ?? null,
    },
  });
}
