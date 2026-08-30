import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Clock3, Database, MessageCircleQuestion, ShieldCheck, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Tabs, TabPanel } from "@/components/ui/Tabs";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { ChatMessage } from "@/components/domain/ChatMessage";
import { askQuestion, getQuestionHistory } from "@/api/ask";
import type { ApiError } from "@/types/common";
import type { ChatTurn } from "@/types/ask";
import { formatDateTime } from "@/utils/date";
import { formatPercent } from "@/utils/format";

const STARTER_QUESTIONS = [
  "What changed recently in the payments service?",
  "Have we seen this error before?",
  "Summarize the latest critical incidents.",
  "Which services have the most knowledge gaps?",
];

let turnCounter = 0;

export function AskPage() {
  const [tab, setTab] = useState<"chat" | "history">("chat");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [query, setQuery] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const historyQuery = useQuery({
    queryKey: ["ask-history"],
    queryFn: () => getQuestionHistory(50, 0),
    enabled: tab === "history",
  });

  async function submitQuestion(questionValue: string) {
    const question = questionValue.trim();
    if (!question || isSubmitting) return;
    const turnId = `turn-${++turnCounter}`;
    setTurns((previous) => [...previous, { id: turnId, question, isPending: true }]);
    setQuery("");
    setIsSubmitting(true);
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
    try {
      const response = await askQuestion(question);
      setTurns((previous) => previous.map((turn) => turn.id === turnId ? { ...turn, isPending: false, response } : turn));
    } catch (error) {
      const apiError = error as ApiError;
      setTurns((previous) => previous.map((turn) => turn.id === turnId ? { ...turn, isPending: false, error: apiError?.message ?? "Something went wrong." } : turn));
    } finally {
      setIsSubmitting(false);
      requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
    }
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    void submitQuestion(query);
  }

  return (
    <div className="flex min-h-[calc(100vh-7rem)] flex-col gap-4">
      <PageHeader title="Ask EKIP" description="Get evidence-backed answers from your engineering knowledge, incidents, code, and conversations." />
      <Tabs items={[{ key: "chat", label: "Ask" }, { key: "history", label: "History" }]} activeKey={tab} onChange={(key) => setTab(key as "chat" | "history")} idPrefix="ask" />

      {tab === "chat" && (
        <TabPanel idPrefix="ask" tabKey="chat" className="flex min-h-0 flex-1 flex-col focus:outline-none">
          <div className="flex-1 overflow-y-auto pb-4 scrollbar-thin" aria-live="polite">
            {turns.length === 0 ? (
              <div className="mx-auto flex max-w-3xl flex-col items-center px-2 py-8 text-center sm:py-14">
                <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent text-white shadow-lg shadow-blue-200">
                  <Sparkles className="h-6 w-6" />
                </div>
                <h2 className="text-2xl font-semibold tracking-tight text-ink">What do you need to know?</h2>
                <p className="mt-2 max-w-xl text-sm leading-6 text-ink-muted">Ask a question in plain language. EKIP searches connected sources, checks evidence, and shows exactly what supports the response.</p>
                <div className="mt-7 grid w-full gap-2 sm:grid-cols-2">
                  {STARTER_QUESTIONS.map((question) => (
                    <button key={question} type="button" onClick={() => void submitQuestion(question)} className="group flex min-h-14 items-center justify-between gap-3 rounded-xl border border-border bg-white px-4 py-3 text-left text-sm text-ink shadow-subtle transition hover:-translate-y-0.5 hover:border-accent-border hover:shadow-panel">
                      <span>{question}</span><ArrowUp className="h-4 w-4 shrink-0 rotate-45 text-ink-subtle group-hover:text-accent" />
                    </button>
                  ))}
                </div>
                <div className="mt-6 flex flex-wrap justify-center gap-x-5 gap-y-2 text-xs text-ink-subtle">
                  <span className="inline-flex items-center gap-1.5"><Database className="h-3.5 w-3.5" />Connected knowledge</span>
                  <span className="inline-flex items-center gap-1.5"><ShieldCheck className="h-3.5 w-3.5" />Citation grounded</span>
                  <span className="inline-flex items-center gap-1.5"><MessageCircleQuestion className="h-3.5 w-3.5" />Confidence routed</span>
                </div>
              </div>
            ) : (
              <div className="mx-auto flex max-w-4xl flex-col gap-7 pb-3 pt-2">
                {turns.map((turn) => <ChatMessage key={turn.id} turn={turn} onRetry={() => void submitQuestion(turn.question)} />)}
                <div ref={bottomRef} />
              </div>
            )}
          </div>

          <form onSubmit={handleSubmit} className="mx-auto w-full max-w-4xl pt-2">
            <div className="rounded-2xl border border-border-strong bg-white p-2 shadow-panel focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/10">
              <textarea
                ref={textareaRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submitQuestion(query); }
                }}
                placeholder="Ask EKIP anything about your systems…"
                aria-label="Question for EKIP"
                autoFocus
                disabled={isSubmitting}
                rows={2}
                className="max-h-36 min-h-[52px] w-full resize-none border-0 bg-transparent px-2.5 py-2 text-sm leading-5 text-ink outline-none placeholder:text-ink-subtle disabled:opacity-60"
              />
              <div className="flex items-center justify-between gap-3 px-1">
                <span className="hidden text-[11px] text-ink-subtle sm:block">Enter to send · Shift + Enter for a new line</span>
                <Button type="submit" variant="primary" isLoading={isSubmitting} disabled={!query.trim()} className="ml-auto rounded-xl">
                  {!isSubmitting && <ArrowUp className="h-4 w-4" />}<span className="sr-only sm:not-sr-only">Ask</span>
                </Button>
              </div>
            </div>
            <p className="mt-2 text-center text-[11px] text-ink-subtle">Responses can be incomplete. Verify critical decisions against the cited sources.</p>
          </form>
        </TabPanel>
      )}

      {tab === "history" && (
        <TabPanel idPrefix="ask" tabKey="history" className="flex-1 overflow-y-auto focus:outline-none scrollbar-thin">
          {historyQuery.isLoading && <LoadingState label="Loading history…" />}
          {historyQuery.isError && <ErrorState onRetry={() => historyQuery.refetch()} />}
          {historyQuery.data?.length === 0 && <EmptyState icon={Clock3} title="No questions yet" description="Questions you ask EKIP will show up here." />}
          {historyQuery.data && historyQuery.data.length > 0 && (
            <ul className="mx-auto flex max-w-4xl flex-col gap-2">
              {historyQuery.data.map((entry) => (
                <li key={entry.id}>
                  <button type="button" onClick={() => { const question = entry.inputSummary?.query; if (question) { setQuery(question); setTab("chat"); requestAnimationFrame(() => textareaRef.current?.focus()); } }} className="flex w-full flex-col gap-1.5 rounded-xl border border-border bg-white px-4 py-3 text-left shadow-subtle hover:border-accent-border hover:bg-accent-subtle/40">
                    <div className="flex items-center justify-between gap-3"><p className="truncate text-sm font-medium text-ink">{entry.inputSummary?.query ?? "(no query recorded)"}</p><span className="shrink-0 text-xs text-ink-subtle">{formatDateTime(entry.startedAt)}</span></div>
                    <div className="flex items-center gap-2 text-xs text-ink-muted"><span className="capitalize">{entry.status}</span>{entry.confidenceScore !== null && <><span className="text-ink-subtle">·</span><span>{formatPercent(entry.confidenceScore)} confidence</span></>}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </TabPanel>
      )}
    </div>
  );
}
