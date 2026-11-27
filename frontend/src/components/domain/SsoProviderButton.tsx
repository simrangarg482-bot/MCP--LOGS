import type { ButtonHTMLAttributes } from "react";
import { Button } from "@/components/ui/Button";
import { providerBrand, type SsoProvider } from "@/utils/sso";

/** Small, self-contained marks (no external image requests on a sign-in page). */
function ProviderMark({ provider }: { provider: SsoProvider }) {
  switch (provider) {
    case "entra_id":
      return (
        <svg aria-hidden="true" viewBox="0 0 21 21" className="h-4 w-4">
          <rect x="1" y="1" width="9" height="9" fill="#f25022" />
          <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
          <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
          <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
        </svg>
      );
    case "google_workspace":
      return (
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4">
          <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5a5.6 5.6 0 0 1-2.4 3.6v3h3.9c2.3-2.1 3.5-5.2 3.5-8.8z" />
          <path fill="#34A853" d="M12 24c3.2 0 6-1.1 8-2.9l-3.9-3c-1.1.7-2.5 1.2-4.1 1.2-3.1 0-5.8-2.1-6.7-5H1.3v3.1A12 12 0 0 0 12 24z" />
          <path fill="#FBBC05" d="M5.3 14.3a7.2 7.2 0 0 1 0-4.6V6.6H1.3a12 12 0 0 0 0 10.8l4-3.1z" />
          <path fill="#EA4335" d="M12 4.8c1.8 0 3.3.6 4.6 1.8l3.4-3.4A12 12 0 0 0 1.3 6.6l4 3.1c.9-2.8 3.6-4.9 6.7-4.9z" />
        </svg>
      );
    case "okta":
      return (
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4">
          <circle cx="12" cy="12" r="9" fill="none" stroke="#007dc1" strokeWidth="5" />
        </svg>
      );
    case "auth0":
      return (
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4">
          <path fill="#eb5424" d="M21.2 8.7 19 2h-7l2.2 6.7h7zM12 2H5l-2.2 6.7L8.5 13 12 2zm0 0 3.5 11 5.7-4.3L19 2h-7zM8.5 13 12 22l9-6.7-5.5-2.3L12 22l-3.5-9z" />
        </svg>
      );
  }
}

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  provider: SsoProvider;
  isLoading?: boolean;
}

/** "Continue with <provider>" -- label and mark come from the provider the organization configured. */
export function SsoProviderButton({ provider, isLoading, ...props }: Props) {
  const brand = providerBrand(provider);
  return (
    <Button type="button" variant="secondary" className="w-full" isLoading={isLoading} {...props}>
      {!isLoading && <ProviderMark provider={provider} />}
      {brand.buttonLabel}
    </Button>
  );
}
