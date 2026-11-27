import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import {
  MAX_PASSWORD_LENGTH,
  MIN_PASSWORD_LENGTH,
  PASSWORD_HINT,
  passwordErrorMessage,
  validateNewPassword,
} from "@/utils/password";

/** Settings -> Security. Today: change password. (Two-factor authentication will live here.) */
export function SecuritySettingsPage() {
  const { user, changePassword } = useAuth();
  const { toast } = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (isSubmitting) return;
    const problem = validateNewPassword(next, user?.email);
    if (problem) {
      setError(problem);
      return;
    }
    if (next !== confirm) {
      setError("The two new passwords don't match.");
      return;
    }
    if (next === current) {
      setError("Choose a password different from your current one.");
      return;
    }
    setIsSubmitting(true);
    setError(null);
    try {
      await changePassword({ currentPassword: current, newPassword: next });
      setCurrent("");
      setNext("");
      setConfirm("");
      toast({
        variant: "success",
        title: "Password changed",
        description: "You were signed out of your other devices. This one stays signed in.",
      });
    } catch (err) {
      setError(passwordErrorMessage(err));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Change password</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex max-w-md flex-col gap-4" data-testid="change-password-form">
            <div>
              <label htmlFor="current-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
                Current password
              </label>
              <Input
                id="current-password"
                type="password"
                required
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="new-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
                New password
              </label>
              <Input
                id="new-password"
                type="password"
                required
                autoComplete="new-password"
                minLength={MIN_PASSWORD_LENGTH}
                maxLength={MAX_PASSWORD_LENGTH}
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
              <p className="mt-1 text-xs text-ink-subtle">{PASSWORD_HINT}</p>
            </div>
            <div>
              <label htmlFor="confirm-new-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
                Confirm new password
              </label>
              <Input
                id="confirm-new-password"
                type="password"
                required
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            {error && (
              <p role="alert" className="rounded-md border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
                {error}
              </p>
            )}
            <div>
              <Button type="submit" variant="primary" size="sm" isLoading={isSubmitting}>
                Change password
              </Button>
            </div>
            <p className="text-xs text-ink-subtle">
              Changing your password signs you out of every other device. If you signed in with single sign-on, your
              identity provider manages your password and this form won&apos;t work for your account.
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
