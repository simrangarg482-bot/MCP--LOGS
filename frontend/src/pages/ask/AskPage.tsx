import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUp,
  BookOpenCheck,
  Clock3,
  Database,
  GitPullRequest,
  Lightbulb,
  MessageCircleQuestion,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { ChatMessage } from "@/components/domain/ChatMessage";
import {
  askQuestion,
  getQuestionHistory,
  searchRecentChanges,
  searchSimilarIncidents,
} from "@/api/ask";
import { listIncidents } from "@/api/incidents";
import { listGapReports } from "@/api/knowledge";
import { useToast } from "@/context/ToastContext";
import type { ApiError } from "@/types/common";
import type { ChatTurn, QuickActionKind } from "@/types/ask";
import { formatDateTime } from "@/utils/date";
import { formatPercent } from "@/utils/format";
import { cn } from "@/utils/cn";

interface StarterQuestion {
  label: string;
  description: string;
  /**
   * The canned query text this starter sends. Omitted for
   * `action: "similar_incidents"`: that button has no fixed query -- it
   * uses whatever the user has currently typed into the composer (see
   * `runSimilarIncidentsAction`), since a hardcoded description would be
   * unrelated to whatever incident/symptoms the user is actually
   * investigating.
   */
  question?: string;
  icon: LucideIcon;
  tone: string;
  /**
   * Which capability this quick action should invoke -- see
   * `QuickActionKind`'s docstring (types/ask.ts). Each of the four starter
   * buttons below routes to its own specialized, already-existing endpoint
   * instead of the generic confidence-routed `/ask` flow.
   */
  action: QuickActionKind;
}

const STARTER_QUESTIONS: StarterQuestion[] = [
  {
    label: "Recent changes",
    description: "Trace commits, deploys, and configuration changes",
    question: "What changed recently in the payments service?",
    icon: GitPullRequest,
    tone: "bg-accent-subtle text-accent ring-accent-border",
    action: "recent_changes",
  },
  {
    label: "Similar incidents",
    description: "Compare symptoms with previous investigations",
    icon: Search,
    tone: "bg-info-subtle text-info ring-info-border",
    action: "similar_incidents",
  },
  {
    label: "Incident briefing",
    description: "Review impact, evidence, and resolution status",
    question: "Summarize the latest critical incidents.",
    icon: TriangleAlert,
    tone: "bg-warning-subtle text-warning ring-warning-border",
    action: "incident_briefing",
  },
  {
    label: "Knowledge coverage",
    description: "Find missing or outdated operational guidance",
    question: "Which services have the most knowledge gaps?",
    icon: BookOpenCheck,
    tone: "bg-success-subtle text-success ring-success-border",
    action: "knowledge_coverage",
  },
];

const TAB_ID_PREFIX = "ask";

let turnCounter = 0;

export function AskPage() {
  const [tab, setTab] = useState<"chat" | "history">("chat");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [query, setQuery] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { toast } = useToast();

  const historyQuery = useQuery({
    queryKey: ["ask-history"],
    queryFn: () => getQuestionHistory(50, 0),
    enabled: tab === "history",
  });

  function resizeComposer(element: HTMLTextAreaElement) {
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 144)}px`;
  }

  async function submitQuestion(questionValue: string) {
    const question = questionValue.trim();
    if (!question || isSubmitting) return;
    const turnId = `turn-${++turnCounter}`;
    setTurns((previous) => [...previous, { id: turnId, question, action: "ask", isPending: true }]);
    setQuery("");
    setIsSubmitting(true);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
    try {
      const response = await askQuestion(question);
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === turnId ? { ...turn, isPending: false, response } : turn,
        ),
      );
    } catch (error) {
      const apiError = error as ApiError;
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === turnId
            ? {
                ...turn,
                isPending: false,
                error: apiError?.message ?? "Something went wrong.",
              }
            : turn,
        ),
      );
    } finally {
      setIsSubmitting(false);
      requestAnimationFrame(() => {
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
        textareaRef.current?.focus();
      });
    }
  }

  /**
   * Runs one of the four Ask EKIP quick-action buttons. Each `action` maps
   * to its own specialized, already-existing endpoint (recent-changes /
   * similar-incidents search, the critical-incidents list, or the
   * Knowledge Gap Agent's report list) rather than being funneled through
   * the generic confidence-routed `askQuestion`/`POST /ask` flow those
   * endpoints were never meant to substitute for -- see the docstring on
   * `QuickActionKind` (types/ask.ts) for why.
   */
  async function runStarterAction(question: string, action: QuickActionKind) {
    if (isSubmitting) return;
    const turnId = `turn-${++turnCounter}`;
    setTurns((previous) => [...previous, { id: turnId, question, action, isPending: true }]);
    setIsSubmitting(true);
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));

    function resolve(update: Partial<ChatTurn>) {
      setTurns((previous) =>
        previous.map((turn) => (turn.id === turnId ? { ...turn, isPending: false, ...update } : turn)),
      );
    }

    try {
      switch (action) {
        case "recent_changes": {
          // GitHub commit/PR/issue content has no file extension, so
          // ingestion classifies and stores it under "documentation", not
          // "code" (literal source files only) -- see
          // agents_service.search_recent_changes's own docstring.
          const searchResults = await searchRecentChanges(question, { collection: "documentation" });
          resolve({ searchResults });
          break;
        }
        case "similar_incidents": {
          const searchResults = await searchSimilarIncidents(question);
          resolve({ searchResults });
          break;
        }
        case "incident_briefing": {
          const incidentResults = await listIncidents({ severity: "critical" });
          resolve({ incidentResults });
          break;
        }
        case "knowledge_coverage": {
          const gapResults = await listGapReports();
          resolve({ gapResults });
          break;
        }
        case "ask": {
          const response = await askQuestion(question);
          resolve({ response });
          break;
        }
      }
    } catch (error) {
      const apiError = error as ApiError;
      resolve({ error: apiError?.message ?? "Something went wrong." });
    } finally {
      setIsSubmitting(false);
      requestAnimationFrame(() => {
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
        textareaRef.current?.focus();
      });
    }
  }

  /**
   * Similar Incidents has no fixed query (see `StarterQuestion.question`'s
   * docstring) -- it searches for incidents similar to whatever the user
   * has typed into the composer, describing the incident/symptoms they're
   * actually investigating right now. Validated here, before any API call:
   * an empty/whitespace composer has nothing to search for, so this shows
   * a toast asking the user to describe it first rather than sending a
   * blank (or previously hardcoded) description to `/search/similar-incidents`.
   */
  function runSimilarIncidentsAction() {
    const description = query.trim();
    if (!description) {
      toast({
        variant: "warning",
        title: "Describe the incident or symptoms first",
        description: "Type what you're investigating in the composer below, then click Similar Incidents again.",
      });
      return;
    }
    setQuery("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    void runStarterAction(description, "similar_incidents");
  }

  /** Retries a turn using whichever capability originally produced it. */
  function retryTurn(turn: ChatTurn) {
    if (turn.action === "ask") {
      void submitQuestion(turn.question);
    } else {
      void runStarterAction(turn.question, turn.action);
    }
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    void submitQuestion(query);
  }

  function startNewConversation() {
    setTurns([]);
    setQuery("");
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function reuseHistoryQuestion(question: string) {
    setQuery(question);
    setTab("chat");
    requestAnimationFrame(() => {
      if (textareaRef.current) {
        resizeComposer(textareaRef.current);
        textareaRef.current.focus();
      }
    });
  }

  const tabs = [
    { key: "chat" as const, label: "Ask" },
    { key: "history" as const, label: "History" },
  ];

  return (
    // Full-bleed: cancel AppLayout's page padding so the chat surface runs
    // edge-to-edge from just under the Topbar to the bottom of the viewport.
    <div className="-mx-4 -my-5 flex h-[calc(100dvh-4rem)] min-h-[560px] flex-col bg-surface sm:-mx-6 sm:-my-6 lg:-mx-8">
      {/* Workspace toolbar */}
      <div className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border px-3 sm:px-5">
        <div
          role="tablist"
          aria-label="Ask EKIP views"
          className="flex items-center gap-1 rounded-lg bg-slate-100 p-0.5"
        >
          {tabs.map((item) => {
            const isActive = tab === item.key;
            return (
              <button
                key={item.key}
                id={`${TAB_ID_PREFIX}-tab-${item.key}`}
                role="tab"
                type="button"
                aria-selected={isActive}
                aria-controls={`${TAB_ID_PREFIX}-tabpanel-${item.key}`}
                onClick={() => setTab(item.key)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
                  isActive ? "bg-white text-ink shadow-subtle" : "text-ink-muted hover:text-ink",
                )}
              >
                {item.label}
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-2">
          <span className="hidden items-center gap-1.5 rounded-full border border-success-border bg-success-subtle px-2.5 py-1 text-[11px] font-medium text-success sm:inline-flex">
            <span className="h-1.5 w-1.5 rounded-full bg-success" />
            Evidence-grounded
          </span>
          {tab === "chat" && turns.length > 0 && (
            <Button size="sm" variant="secondary" onClick={startNewConversation}>
              <Plus className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">New conversation</span>
              <span className="sm:hidden">New</span>
            </Button>
          )}
        </div>
      </div>

      {/* Chat view */}
      {tab === "chat" && (
        <div
          id={`${TAB_ID_PREFIX}-tabpanel-chat`}
          role="tabpanel"
          aria-labelledby={`${TAB_ID_PREFIX}-tab-chat`}
          tabIndex={0}
          className="flex min-h-0 flex-1 flex-col focus:outline-none"
        >
          <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin" aria-live="polite">
            {turns.length === 0 ? (
              <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col items-center justify-center px-5 py-10 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent text-white shadow-sm">
                  <Sparkles className="h-5 w-5" />
                </div>
                <h2 className="mt-5 text-xl font-semibold tracking-[-0.02em] text-ink">
                  Ask what happened. See why.
                </h2>
                <p className="mt-2 max-w-md text-sm leading-6 text-ink-muted">
                  EKIP searches incidents, code, runbooks, and conversations, then ties every answer
                  back to the evidence that supports it.
                </p>

                <div className="mt-7 grid w-full gap-2.5 sm:grid-cols-2">
                  {STARTER_QUESTIONS.map((starter) => {
                    const Icon = starter.icon;
                    return (
                      <button
                        key={starter.label}
                        type="button"
                        onClick={() => {
                          if (starter.action === "similar_incidents") {
                            runSimilarIncidentsAction();
                          } else {
                            void runStarterAction(starter.question ?? starter.label, starter.action);
                          }
                        }}
                        className="group flex items-start gap-3 rounded-lg border border-border bg-surface p-3 text-left transition-colors hover:border-accent-border hover:bg-accent-subtle/40 focus-visible:border-accent"
                      >
                        <span
                          className={cn(
                            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ring-1 ring-inset",
                            starter.tone,
                          )}
                        >
                          <Icon className="h-4 w-4" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
                            {starter.label}
                            <ArrowUp className="h-3 w-3 shrink-0 rotate-45 text-ink-subtle opacity-0 transition-opacity group-hover:opacity-100" />
                          </span>
                          <span className="mt-0.5 block text-xs leading-5 text-ink-muted">
                            {starter.description}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>

                <div className="mt-7 flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 text-[11px] text-ink-subtle">
                  <span className="inline-flex items-center gap-1.5">
                    <Database className="h-3.5 w-3.5" />
                    Searches connected knowledge
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <ShieldCheck className="h-3.5 w-3.5" />
                    Cites supporting evidence
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <MessageCircleQuestion className="h-3.5 w-3.5" />
                    Escalates uncertainty
                  </span>
                </div>
              </div>
            ) : (
              <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-8 pt-6 sm:px-6">
                {turns.map((turn) => (
                  <ChatMessage
                    key={turn.id}
                    turn={turn}
                    onRetry={() => retryTurn(turn)}
                  />
                ))}
                <div ref={bottomRef} />
              </div>
            )}
          </div>

          {/* Composer */}
          <div className="border-t border-border bg-surface px-3 py-3 sm:px-5">
            <form onSubmit={handleSubmit} className="mx-auto w-full max-w-6xl">
              <div className="rounded-xl border border-border-strong bg-white shadow-subtle transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20">
                <textarea
                  ref={textareaRef}
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    resizeComposer(event.currentTarget);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void submitQuestion(query);
                    }
                  }}
                  placeholder="Ask EKIP anything about your systems…"
                  aria-label="Question for EKIP"
                  rows={1}
                  className="block max-h-36 min-h-[46px] w-full resize-none rounded-t-xl border-0 bg-transparent px-3.5 py-3 text-sm leading-5 text-ink outline-none placeholder:text-ink-subtle focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
                />
                <div className="flex items-center justify-between gap-3 border-t border-border px-2.5 py-2">
                  <span className="hidden min-w-0 items-center gap-1.5 truncate text-[11px] text-ink-subtle sm:inline-flex">
                    <Lightbulb className="h-3 w-3 shrink-0" />
                    Name a service, incident, or timeframe for a sharper answer
                  </span>
                  <Button
                    type="submit"
                    variant="primary"
                    isLoading={isSubmitting}
                    disabled={!query.trim() || isSubmitting}
                    className="ml-auto shrink-0 rounded-lg px-3.5"
                  >
                    {!isSubmitting && <ArrowUp className="h-4 w-4" />}
                    <span>{isSubmitting ? "Checking evidence" : "Ask EKIP"}</span>
                  </Button>
                </div>
              </div>
              <p className="mt-2 text-center text-[11px] text-ink-subtle">
                Enter to send · Shift + Enter for a new line
              </p>
            </form>
          </div>
        </div>
      )}

      {/* History view */}
      {tab === "history" && (
        <div
          id={`${TAB_ID_PREFIX}-tabpanel-history`}
          role="tabpanel"
          aria-labelledby={`${TAB_ID_PREFIX}-tab-history`}
          tabIndex={0}
          className="min-h-0 flex-1 overflow-y-auto bg-background focus:outline-none scrollbar-thin"
        >
          <div className="mx-auto max-w-3xl px-4 py-7 sm:px-6">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-ink">Question history</h2>
              <p className="mt-1 text-xs leading-5 text-ink-muted">
                Reuse a previous question or review how confidently EKIP handled it.
              </p>
            </div>

            {historyQuery.isLoading && <LoadingState label="Loading history…" />}
            {historyQuery.isError && <ErrorState onRetry={() => historyQuery.refetch()} />}
            {historyQuery.data?.length === 0 && (
              <div className="rounded-xl border border-dashed border-border-strong bg-surface py-8">
                <EmptyState
                  icon={Clock3}
                  title="No questions yet"
                  description="Questions you ask EKIP will show up here."
                />
              </div>
            )}
            {historyQuery.data && historyQuery.data.length > 0 && (
              <ul className="flex flex-col gap-1.5">
                {historyQuery.data.map((entry) => {
                  const question = entry.inputSummary?.query;
                  return (
                    <li key={entry.id}>
                      <button
                        type="button"
                        disabled={!question}
                        onClick={() => question && reuseHistoryQuestion(question)}
                        className="group flex w-full items-start gap-3 rounded-lg border border-border bg-surface px-3.5 py-3 text-left transition-colors hover:border-accent-border hover:bg-accent-subtle/40 disabled:cursor-default disabled:hover:border-border disabled:hover:bg-surface"
                      >
                        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-slate-100 text-ink-muted">
                          <MessageCircleQuestion className="h-3.5 w-3.5" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-start justify-between gap-3">
                            <span className="line-clamp-2 text-[13px] font-medium leading-5 text-ink">
                              {question ?? "(no query recorded)"}
                            </span>
                            <span className="shrink-0 text-[11px] text-ink-subtle">
                              {formatDateTime(entry.startedAt)}
                            </span>
                          </span>
                          <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                            <span
                              className={cn(
                                "h-1.5 w-1.5 rounded-full",
                                entry.status === "succeeded"
                                  ? "bg-success"
                                  : entry.status === "failed"
                                    ? "bg-critical"
                                    : "bg-warning",
                              )}
                            />
                            <span className="capitalize">{entry.status}</span>
                            {entry.confidenceScore !== null && (
                              <>
                                <span className="text-ink-subtle">·</span>
                                <span>{formatPercent(entry.confidenceScore)} confidence</span>
                              </>
                            )}
                            {question && (
                              <span className="ml-auto inline-flex items-center gap-1 font-medium text-accent opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                                Use again
                                <ArrowUp className="h-3 w-3 rotate-45" />
                              </span>
                            )}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
