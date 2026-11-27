import { Link, useNavigate } from "react-router-dom";
import { PauseCircle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { clearSuspensionNotice, getSuspensionNotice } from "@/context/suspensionNotice";

/**
 * Explains why an organization cannot be used. Public and static on purpose: a suspended session cannot
 * call any authenticated API, so this page calls none -- and a page that redirects only to itself or to
 * /login cannot loop. It shows the organization's name and the customer-facing message the platform
 * operator chose to share; the operator's reason and private note never reach the browser.
 */
export function SuspendedPage() {
  const navigate = useNavigate();
  const notice = getSuspensionNotice();

  function signInElsewhere() {
    // Forget the notice so /login (and the protected routes) behave normally again, then let the person
    // sign in to a different organization if they belong to one.
    clearSuspensionNotice();
    navigate("/login");
  }

  return (
    <div data-testid="suspended-page">
      <SuspendedBody
        organizationName={notice?.organizationName ?? null}
        customerMessage={notice?.customerMessage ?? null}
        onSignIn={signInElsewhere}
      />
    </div>
  );
}

function SuspendedBody(props: {
  organizationName: string | null;
  customerMessage: string | null;
  onSignIn: () => void;
}) {
  return (
    <>
      <div className="glass-panel rounded-2xl px-6 py-6 text-center">
        <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-critical-subtle">
          <PauseCircle className="h-5 w-5 text-critical" aria-hidden="true" />
        </div>
        <h1 className="text-lg font-semibold text-ink">
          {props.organizationName ? `${props.organizationName} is suspended` : "Your organization is suspended"}
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          Access to this organization is unavailable right now, so you cannot sign in to it or use it. Your
          data has not been deleted.
        </p>
        {props.customerMessage && (
          <blockquote
            className="mt-4 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-left text-sm text-ink"
            data-testid="suspension-message"
          >
            {props.customerMessage}
          </blockquote>
        )}
        <p className="mt-4 text-xs text-ink-subtle">
          Contact your organization&apos;s administrator or EKIP support to restore access.
        </p>
        <div className="mt-5 flex flex-col gap-2">
          <Button variant="primary" onClick={props.onSignIn}>
            Sign in to a different organization
          </Button>
          <Link to="/about" className="text-xs font-medium text-ink-muted hover:text-ink">
            What is EKIP?
          </Link>
        </div>
      </div>
    </>
  );
}
