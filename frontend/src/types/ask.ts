import type { ISODateString, UUID } from "./common";
import type { Incident } from "./incident";
import type { GapReport } from "./knowledge";

export interface Citation {
  documentId: UUID;
  chunkId: UUID;
  sourceUrl: string | null;
  excerpt: string;
}

export type EvidenceSource =
  | "github"
  | "pull_request"
  | "commit"
  | "issue"
  | "slack"
  | "jira"
  | "deployment"
  | "postmortem"
  | "monitoring";

export interface EvidenceItem {
  source: EvidenceSource;
  reference: string;
  summary: string;
  retrievedAt: ISODateString;
  sourceTimestamp: ISODateString | null;
  metadata: Record<string, string>;
}

export interface RootCauseHypothesis {
  description: string;
  confidence: number;
  supportingEvidenceIds: string[];
}

export interface InvestigationResult {
  evidence: EvidenceItem[];
  hypotheses: RootCauseHypothesis[];
  suggestedOwnerTeam: string | null;
  suggestedNextSteps: string[];
}

export interface AskResponse {
  confidence: number;
  routeTaken: "answer" | "investigation";
  answer: string | null;
  /**
   * The production pipeline's own authoritative outcome for the answer
   * path (Priority 10) -- "answered" (a substantive, grounded answer) or
   * "no_answer" (the system intentionally declined; evidence was
   * insufficient or the draft could not be grounded). `null` when no
   * answer-path decision was made (the investigation route, an
   * infrastructure-failure fallback, or a response from before this field
   * existed) -- never assume `null` means "answered".
   */
  answerMode: "answered" | "no_answer" | null;
  citations: Citation[];
  investigation: InvestigationResult | null;
}

export type AgentExecutionStatus = "running" | "succeeded" | "failed";

export interface QuestionHistoryEntry {
  id: UUID;
  organizationId: UUID;
  agentName: string;
  triggerSource: string;
  inputSummary: Record<string, string | null> | null;
  confidenceScore: number | null;
  status: AgentExecutionStatus;
  errorDetail: string | null;
  startedAt: ISODateString;
  completedAt: ISODateString | null;
}

export interface ScoredChunk {
  chunkId: UUID;
  documentId: UUID;
  collection: "documentation" | "code" | "conversations";
  content: string;
  score: number;
  sourceOffsetStart: number;
  sourceOffsetEnd: number;
  title: string | null;
  sourceUrl: string | null;
  metadata: Record<string, string>;
}

/**
 * Which capability produced a chat turn. `"ask"` is the generic,
 * confidence-routed `POST /ask` flow (free-text composer input, and
 * history reuse). The other four are the Ask EKIP quick-action buttons,
 * each of which calls its own specialized, already-existing endpoint
 * instead of being funneled through `/ask` -- see the starter buttons'
 * definitions in `pages/ask/AskPage.tsx`.
 */
export type QuickActionKind =
  | "ask"
  | "recent_changes"
  | "similar_incidents"
  | "incident_briefing"
  | "knowledge_coverage";

/**
 * One turn in the Ask EKIP chat UI -- a question plus, once resolved, its
 * result. Exactly one of `response` / `searchResults` / `incidentResults`
 * / `gapResults` is populated once a turn resolves successfully, chosen by
 * `action`: `"ask"` populates `response` (the existing confidence-routed
 * `AskResponse` shape); the quick-action kinds each populate the field
 * matching their own endpoint's real return type, rather than being
 * force-fit into `AskResponse`'s answer/investigation shape, which doesn't
 * apply to a raw search or a listing.
 */
export interface ChatTurn {
  id: string;
  question: string;
  action: QuickActionKind;
  isPending: boolean;
  response?: AskResponse;
  searchResults?: ScoredChunk[];
  incidentResults?: Incident[];
  gapResults?: GapReport[];
  error?: string;
}
