import { useState } from "react";
import { transitionOrganization } from "@/api/platform";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import type { ApiError } from "@/types/common";
import type { LifecycleAction, PlatformOrganizationDetail, StatusChangeResult } from "@/types/platform";
import { MESSAGE_MAX, NOTE_MAX, REASON_MAX, validateOptionalText, validateReason } from "@/utils/platform";

const COPY: Record<LifecycleAction, { title: string; confirm: string; verb: string; danger: boolean; help: string }> = {
  activate: {
    title: "Activate organization",
    confirm: "Activate organization",
    verb: "activated",
    danger: false,
    help: "Marks this onboarding organization as active. Nobody is signed out.",
  },
  suspend: {
    title: "Suspend organization",
    confirm: "Suspend organization",
    verb: "suspended",
    danger: true,
    help: "Everyone in this organization is signed out immediately and cannot sign in, use the API or the MCP tools, or run background jobs until it is reactivated. Their data is kept. The same people's other organizations are not affected.",
  },
  reactivate: {
    title: "Reactivate organization",
    confirm: "Reactivate organization",
    verb: "reactivated",
    danger: false,
    help: "Restores access. Sessions from before the suspension stay ended: everyone signs in again.",
  },
};

const FIELD =
  "w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-sm text-ink placeholder:text-ink-subtle focus-visible:border-accent";

interface Props {
  action: LifecycleAction;
  organization: PlatformOrganizationDetail;
  onClose: () => void;
  onDone: (result: StatusChangeResult) => void;
}

export function LifecycleDialog({ action, organization, onClose, onDone }: Props) {
  const copy = COPY[action];
  const [reason, setReason] = useState("");
  const [customerMessage, setCustomerMessage] = useState("");
  const [internalNote, setInternalNote] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const reasonError = validateReason(reason);
  const messageError = action === "suspend" ? validateOptionalText(customerMessage, MESSAGE_MAX, "The customer message") : null;
  const noteError = action === "suspend" ? validateOptionalText(internalNote, NOTE_MAX, "The internal note") : null;
  const confirmError = copy.danger && !understood ? "Confirm that you understand the effect." : null;
  const firstError = reasonError ?? messageError ?? noteError ?? confirmError;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setAttempted(true);
    if (firstError) return;
    setSubmitting(true);
    setServerError(null);
    try {
      const result = await transitionOrganization(organization.id, action, { reason, customerMessage, internalNote });
      onDone(result);
    } catch (err) {
      const e = err as ApiError;
      setServerError(
        e.errorCode === "organization.already_in_status" || e.errorCode === "organization.invalid_transition"
          ? `${e.message} The organization may have been changed by someone else; close this and refresh.`
          : e.errorCode === "platform.admin_required"
            ? "Only platform administrators can change an organization's status."
            : e.errorCode?.startsWith("rate_limited")
              ? "Too many changes in a short time. Wait a moment and try again."
              : (e.message ?? "The change could not be made."),
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal open onClose={onClose} title={copy.title} description={organization.name}>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <p className="text-xs text-ink-muted">{copy.help}</p>

        <div>
          <label htmlFor="lifecycle-reason" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Reason (required, recorded in the audit history)
          </label>
          <textarea
            id="lifecycle-reason"
            className={FIELD}
            rows={3}
            maxLength={REASON_MAX + 200}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            aria-invalid={attempted && Boolean(reasonError)}
          />
          {attempted && reasonError && (
            <p role="alert" className="mt-1 text-xs text-critical">
              {reasonError}
            </p>
          )}
        </div>

        {action === "suspend" && (
          <>
            <div>
              <label htmlFor="lifecycle-message" className="mb-1.5 block text-xs font-medium text-ink-muted">
                Message to the customer (optional, shown to their members and administrators)
              </label>
              <textarea
                id="lifecycle-message"
                className={FIELD}
                rows={2}
                value={customerMessage}
                onChange={(e) => setCustomerMessage(e.target.value)}
              />
              {attempted && messageError && (
                <p role="alert" className="mt-1 text-xs text-critical">
                  {messageError}
                </p>
              )}
            </div>
            <div>
              <label htmlFor="lifecycle-note" className="mb-1.5 block text-xs font-medium text-ink-muted">
                Internal note (optional, private to platform operators, never shown to the customer)
              </label>
              <textarea
                id="lifecycle-note"
                className={FIELD}
                rows={2}
                value={internalNote}
                onChange={(e) => setInternalNote(e.target.value)}
              />
              {attempted && noteError && (
                <p role="alert" className="mt-1 text-xs text-critical">
                  {noteError}
                </p>
              )}
            </div>
            <label className="flex items-start gap-2 text-xs text-ink">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={understood}
                onChange={(e) => setUnderstood(e.target.checked)}
              />
              <span>
                I understand that every member of <strong>{organization.name}</strong> will be signed out immediately.
              </span>
            </label>
            {attempted && confirmError && (
              <p role="alert" className="-mt-2 text-xs text-critical">
                {confirmError}
              </p>
            )}
          </>
        )}

        {serverError && (
          <p role="alert" className="text-xs text-critical">
            {serverError}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" size="sm" variant={copy.danger ? "danger" : "primary"} isLoading={submitting}>
            {copy.confirm}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
