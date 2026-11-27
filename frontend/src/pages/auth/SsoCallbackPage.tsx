import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import {
  clearPendingSsoLogin,
  readPendingSsoLogin,
  providerErrorMessage,
  ssoCallbackUrl,
  ssoErrorMessage,
} from "@/utils/sso";

interface CallbackParams {
  code: string | null;
  state: string | null;
  error: string | null;
}

type Outcome = { ok: true } | { ok: false; title: string; description: string };

// The page's work must happen exactly once per sign-in, even though React 18 StrictMode (development)
// mounts, unmounts and re-mounts every component, running each effect twice. An authorization
// code is single-use: a second exchange would fail and, worse, look like a failed login. So the
// URL parameters are captured -- and removed from the address bar -- the first time anything asks,
// and the exchange itself is one shared promise per organization. Both effect runs await the same
// promise; only the first starts the work. Entries are dropped once the work has settled.
const captured = new Map<string, CallbackParams>();
const runs = new Map<string, Promise<Outcome>>();

function captureParams(slug: string): CallbackParams | null {
  const existing = captured.get(slug);
  if (existing) return existing;
  const query = new URLSearchParams(window.location.search);
  const params: CallbackParams = { code: query.get("code"), state: query.get("state"), error: query.get("error") };
  // Remove the code/state (and any fragment) from the address bar and history IMMEDIATELY.
  if (window.location.search || window.location.hash) {
    window.history.replaceState(null, "", window.location.pathname);
  }
  if (!params.code && !params.state && !params.error) return null;
  captured.set(slug, params);
  return params;
}

function failure(title: string, description: string): Outcome {
  return { ok: false, title, description };
}

type Complete = ReturnType<typeof useAuth>["completeSsoLogin"];

async function execute(slug: string, params: CallbackParams | null, complete: Complete): Promise<Outcome> {
  const pending = readPendingSsoLogin();
  // The reference to the server-side attempt is single-use here too: gone before anything else happens.
  clearPendingSsoLogin();

  if (params?.error) {
    const message = providerErrorMessage(params.error);
    return failure(message.title, message.description);
  }
  if (!params?.code || !params.state) {
    return failure("Nothing to complete", "This page is only used while signing in. Start from the sign-in page.");
  }
  if (!pending || pending.slug !== slug) {
    return failure(
      "This sign-in attempt can't be continued",
      "It expired, or it was started in a different browser tab or window. Start again.",
    );
  }
  if (pending.state !== params.state) {
    return failure("Sign-in response didn't match", "This response doesn't belong to the attempt you started. Start again.");
  }
  try {
    await complete(
      { orgSlug: slug, code: params.code, state: params.state, transactionId: pending.transactionId },
      ssoCallbackUrl(slug),
    );
    return { ok: true };
  } catch (err) {
    return failure("We couldn't sign you in", ssoErrorMessage(err));
  }
}

/** `/o/:slug/callback` -- where the identity provider sends the browser back. */
export function SsoCallbackPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const { completeSsoLogin } = useAuth();
  const navigate = useNavigate();
  const [failed, setFailed] = useState<Extract<Outcome, { ok: false }> | null>(null);

  useEffect(() => {
    let cancelled = false;
    let run = runs.get(slug);
    if (!run) {
      const params = captureParams(slug);
      run = execute(slug, params, completeSsoLogin).finally(() => {
        captured.delete(slug);
        runs.delete(slug);
      });
      runs.set(slug, run);
    }
    void run.then((outcome) => {
      if (cancelled) return; // the StrictMode throw-away mount
      if (outcome.ok) navigate("/ask", { replace: true });
      else setFailed(outcome);
    });
    return () => {
      cancelled = true;
    };
  }, [slug, completeSsoLogin, navigate]);

  if (failed) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6" data-testid="sso-callback-error">
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">{failed.title}</h1>
        <p role="alert" className="mb-5 text-center text-sm text-ink-muted">
          {failed.description}
        </p>
        <div className="flex flex-col gap-2 text-center text-xs">
          <Link to={`/o/${slug}/login`} className="font-medium text-accent hover:underline">
            Try again
          </Link>
          <Link to="/login" className="font-medium text-ink-subtle hover:text-ink">
            Back to sign in
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="glass-panel flex flex-col items-center gap-3 rounded-2xl px-6 py-8" data-testid="sso-callback-working">
      <Loader2 className="h-5 w-5 animate-spin text-accent" aria-hidden="true" />
      <p className="text-sm text-ink-muted" role="status">
        Completing sign-in…
      </p>
    </div>
  );
}
