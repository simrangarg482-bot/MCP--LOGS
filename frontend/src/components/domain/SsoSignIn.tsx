import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { beginSsoLogin, getSsoOptions } from "@/api/auth";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { SsoProviderButton } from "@/components/domain/SsoProviderButton";
import type { SsoOptions } from "@/types/auth";
import {
  isSafeRedirectTarget,
  isValidSlug,
  normalizeSlug,
  savePendingSsoLogin,
  ssoCallbackUrl,
  ssoErrorMessage,
} from "@/utils/sso";

type Phase = "idle" | "checking" | "available" | "unavailable" | "starting";

interface Props {
  /** When set (the `/o/:slug/login` page) the organization is fixed and checked on mount. */
  fixedSlug?: string;
}

/**
 * "Sign in with SSO": names the organization, asks the server whether SSO is usable for it, and
 * offers ONLY the provider that organization configured and enabled. Unknown organizations and
 * ones without SSO get the same neutral message (the server answers them identically).
 */
export function SsoSignIn({ fixedSlug }: Props) {
  const [slugInput, setSlugInput] = useState(fixedSlug ?? "");
  const [slug, setSlug] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [options, setOptions] = useState<SsoOptions | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A ref, not state: a double click fires twice before React re-renders.
  const startingRef = useRef(false);
  const requestRef = useRef(0);

  const check = useCallback(async (rawSlug: string) => {
    const candidate = normalizeSlug(rawSlug);
    setError(null);
    setOptions(null);
    if (!isValidSlug(candidate)) {
      setSlug(null);
      setPhase("idle");
      setError("Enter your organization's short name, e.g. acme or acme-corp.");
      return;
    }
    const request = ++requestRef.current;
    setSlug(candidate);
    setPhase("checking");
    try {
      const result = await getSsoOptions(candidate);
      if (request !== requestRef.current) return; // a newer check superseded this one
      setOptions(result);
      setPhase(result.available && result.provider ? "available" : "unavailable");
    } catch (err) {
      if (request !== requestRef.current) return;
      setPhase("idle");
      setError(ssoErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    if (fixedSlug) void check(fixedSlug);
  }, [fixedSlug, check]);

  async function start() {
    if (startingRef.current || !slug || !options?.provider) return;
    startingRef.current = true;
    setPhase("starting");
    setError(null);
    try {
      const begun = await beginSsoLogin(slug, ssoCallbackUrl(slug));
      if (!isSafeRedirectTarget(begun.authorizationUrl)) {
        throw { errorCode: "auth.idp_response_invalid" };
      }
      if (!savePendingSsoLogin({ slug, state: begun.state, transactionId: begun.transactionId })) {
        // Without somewhere to keep the reference the callback could not be completed.
        throw { message: "Your browser is blocking storage, which sign-in needs. Allow it for this site and try again." };
      }
      window.location.assign(begun.authorizationUrl);
      // Stay in the "starting" state: the page is being replaced. Do not release the guard.
    } catch (err) {
      startingRef.current = false;
      setPhase("available");
      setError(ssoErrorMessage(err));
    }
  }

  const showForm = !fixedSlug;

  return (
    <div data-testid="sso-signin" className="flex flex-col gap-3">
      {showForm && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void check(slugInput);
          }}
        >
          <label htmlFor="sso-org" className="block text-xs font-medium text-ink-muted">
            Organization
          </label>
          <div className="flex gap-2">
            <Input
              id="sso-org"
              autoComplete="organization"
              placeholder="your-organization"
              value={slugInput}
              onChange={(e) => {
                setSlugInput(e.target.value);
                if (phase !== "idle") {
                  setPhase("idle");
                  setOptions(null);
                  setSlug(null);
                }
              }}
            />
            <Button type="submit" variant="secondary" isLoading={phase === "checking"} disabled={!slugInput.trim()}>
              Continue
            </Button>
          </div>
        </form>
      )}

      {fixedSlug && phase === "checking" && <p className="text-center text-sm text-ink-muted">Checking sign-in options…</p>}

      {(phase === "available" || phase === "starting") && options?.provider && (
        <div className="flex flex-col gap-2">
          {fixedSlug && (
            <p className="text-center text-xs text-ink-subtle">
              Sign in to <span className="font-medium text-ink">{fixedSlug}</span>
            </p>
          )}
          <SsoProviderButton provider={options.provider} isLoading={phase === "starting"} onClick={() => void start()} />
        </div>
      )}

      {phase === "unavailable" && (
        <p role="status" className="rounded-md border border-border bg-white/[0.03] px-3 py-2 text-center text-xs text-ink-muted">
          Single sign-on isn&apos;t available for this organization.{" "}
          {fixedSlug ? (
            <Link to="/login" className="font-medium text-accent hover:underline">
              Sign in with a password
            </Link>
          ) : (
            "Check the name, or sign in with your email and password."
          )}
        </p>
      )}

      {error && (
        <p role="alert" className="rounded-md border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
          {error}
        </p>
      )}
    </div>
  );
}
