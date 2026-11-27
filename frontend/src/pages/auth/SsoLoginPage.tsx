import { Link, useParams } from "react-router-dom";
import { SsoSignIn } from "@/components/domain/SsoSignIn";
import { isValidSlug, normalizeSlug } from "@/utils/sso";

/** `/o/:slug/login` -- the shareable, organization-specific entry point to single sign-on. */
export function SsoLoginPage() {
  const { slug: rawSlug } = useParams<{ slug: string }>();
  const slug = normalizeSlug(rawSlug ?? "");

  return (
    <div className="glass-panel rounded-2xl px-6 py-6">
      <h1 className="mb-4 text-center text-lg font-semibold text-ink">Single sign-on</h1>
      {isValidSlug(slug) ? (
        <SsoSignIn key={slug} fixedSlug={slug} />
      ) : (
        <p role="alert" className="text-center text-sm text-ink-muted">
          This sign-in link isn&apos;t valid.
        </p>
      )}
      <p className="mt-6 text-center text-xs text-ink-subtle">
        <Link to="/login" className="font-medium text-accent hover:underline">
          Sign in with email and password instead
        </Link>
      </p>
    </div>
  );
}
