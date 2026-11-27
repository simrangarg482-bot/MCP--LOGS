import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2, ThumbsDown, ThumbsUp } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { getAnswerFeedback, submitAnswerFeedback } from "@/api/feedback";
import type {
  AnswerFeedback as AnswerFeedbackRecord,
  FeedbackReasonCode,
  FeedbackRating,
} from "@/types/ask";
import type { UUID } from "@/types/common";
import {
  FEEDBACK_REASON_OPTIONS,
  MAX_FEEDBACK_COMMENT_LENGTH,
  buildFeedbackPayload,
  feedbackErrorMessage,
  shouldSubmitRating,
  validateFeedbackComment,
} from "@/utils/feedback";
import { cn } from "@/utils/cn";

interface AnswerFeedbackProps {
  /** The `executionId` from the `AskResponse` being rated. */
  executionId: UUID;
}

/**
 * Helpful / Not helpful controls for one AI response.
 *
 * - Feedback is attached to the EXECUTION (not the question text), so the same
 *   question asked twice is rated independently.
 * - The saved rating is read back from the server, so it survives a reload and
 *   is shown as pressed; clicking the other button changes it, clicking the
 *   same one does nothing (no duplicate record, no duplicate request).
 * - A rating is saved the moment it is clicked. Explaining a negative rating is
 *   offered, never required: the details form appears after saving and can be
 *   skipped.
 */
export function AnswerFeedback({ executionId }: AnswerFeedbackProps) {
  const queryClient = useQueryClient();
  const queryKey = ["answer-feedback", executionId] as const;

  const existing = useQuery({
    queryKey,
    queryFn: () => getAnswerFeedback(executionId),
    // A failed read must not block rating: treat it as "nothing saved yet".
    retry: false,
    staleTime: Infinity,
  });
  const saved: AnswerFeedbackRecord | null = existing.data ?? null;

  const [detailsOpen, setDetailsOpen] = useState(false);
  const [reason, setReason] = useState<FeedbackReasonCode | null>(null);
  const [comment, setComment] = useState("");
  const [detailsSaved, setDetailsSaved] = useState(false);
  const [justSaved, setJustSaved] = useState(false);

  const mutation = useMutation({
    mutationFn: (variables: { rating: FeedbackRating; reason: FeedbackReasonCode | null; comment: string }) =>
      submitAnswerFeedback(
        executionId,
        buildFeedbackPayload(variables.rating, variables.reason, variables.comment),
      ),
    onSuccess: (record) => {
      queryClient.setQueryData(queryKey, record);
      setJustSaved(true);
    },
  });

  const commentError = validateFeedbackComment(comment);
  const isPending = mutation.isPending;
  const controlsDisabled = isPending || existing.isLoading;

  function rate(rating: FeedbackRating) {
    if (!shouldSubmitRating(saved?.rating ?? null, rating, isPending)) return;
    setJustSaved(false);
    setDetailsSaved(false);
    // Changing to a different rating starts a fresh explanation.
    setReason(null);
    setComment("");
    setDetailsOpen(rating === "not_helpful");
    mutation.mutate({ rating, reason: null, comment: "" });
  }

  function saveDetails() {
    if (!saved || commentError || isPending) return;
    mutation.mutate(
      { rating: "not_helpful", reason, comment },
      {
        onSuccess: () => {
          setDetailsSaved(true);
          setDetailsOpen(false);
        },
      },
    );
  }

  function ratingButton(rating: FeedbackRating, label: string, Icon: typeof ThumbsUp) {
    const selected = saved?.rating === rating;
    return (
      <Button
        size="sm"
        variant={selected ? "primary" : "ghost"}
        aria-pressed={selected}
        disabled={controlsDisabled}
        onClick={() => rate(rating)}
        className={cn(!selected && "text-ink-subtle")}
      >
        <Icon className="h-3.5 w-3.5" />
        {label}
      </Button>
    );
  }

  return (
    <div
      role="group"
      aria-label="Rate this answer"
      data-testid="answer-feedback"
      className="flex flex-col gap-2 border-t border-border px-4 py-2.5 sm:px-5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-ink-subtle">Was this helpful?</span>
        {ratingButton("helpful", "Helpful", ThumbsUp)}
        {ratingButton("not_helpful", "Not helpful", ThumbsDown)}

        <span role="status" aria-live="polite" className="text-xs">
          {isPending && (
            <span className="inline-flex items-center gap-1.5 text-ink-muted">
              <Loader2 className="h-3 w-3 animate-spin" />
              Saving…
            </span>
          )}
          {!isPending && !mutation.isError && (justSaved || detailsSaved) && (
            <span className="inline-flex items-center gap-1.5 text-success">
              <Check className="h-3 w-3" />
              {detailsSaved ? "Thanks — details saved" : "Thanks — feedback saved"}
            </span>
          )}
        </span>
      </div>

      {mutation.isError && !isPending && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 text-xs text-critical"
        >
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          <span>{feedbackErrorMessage(mutation.error)}</span>
          {(mutation.error as { status?: number })?.status !== 404 && (
            <Button size="sm" variant="secondary" onClick={() => mutation.mutate(mutation.variables!)}>
              Retry
            </Button>
          )}
        </div>
      )}

      {saved?.rating === "not_helpful" && !detailsOpen && !mutation.isError && !isPending && (
        <div>
          <button
            type="button"
            onClick={() => {
              setReason(saved.reasonCode);
              setComment(saved.comment ?? "");
              setDetailsOpen(true);
            }}
            className="text-xs text-accent hover:underline"
          >
            {saved.reasonCode || saved.comment ? "Edit details" : "Add details (optional)"}
          </button>
        </div>
      )}

      {detailsOpen && saved?.rating === "not_helpful" && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            saveDetails();
          }}
        >
          <p className="text-xs text-ink-muted">What went wrong? (optional)</p>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Reason">
            {FEEDBACK_REASON_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-pressed={reason === option.value}
                onClick={() => setReason(reason === option.value ? null : option.value)}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-xs transition-colors",
                  reason === option.value
                    ? "border-accent-border bg-accent-subtle text-accent"
                    : "border-border text-ink-muted hover:text-ink",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
          <label className="sr-only" htmlFor={`feedback-comment-${executionId}`}>
            Tell us more (optional)
          </label>
          <textarea
            id={`feedback-comment-${executionId}`}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            maxLength={MAX_FEEDBACK_COMMENT_LENGTH}
            rows={2}
            placeholder="Tell us more (optional) — please don't include passwords or keys"
            className="w-full resize-y rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-sm text-ink placeholder:text-ink-subtle focus:border-accent-border focus:outline-none"
          />
          {commentError && <p className="text-xs text-critical">{commentError}</p>}
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" variant="primary" disabled={isPending || !!commentError}>
              Send details
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setDetailsOpen(false)}>
              Skip
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
