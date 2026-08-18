import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Organization, Project } from "@/types/tenancy";
import { listOrganizations, listProjects } from "@/api/tenancy";
import { useAuth } from "./AuthContext";
import { useToast } from "./ToastContext";

interface TenantContextValue {
  organization: Organization | null;
  project: Project | null;
  projects: Project[];
  isLoading: boolean;
  /** True only when the `GET /organizations`/`GET /organizations/{id}/
   * projects` fetch itself failed (network error, 5xx, etc) -- distinct
   * from `organization === null`/`projects.length === 0`, which are the
   * normal, successful "nothing here" states. Pages that render a generic
   * "no organization selected" message for `!organization` should check
   * this first so a real fetch failure isn't shown as if it were routine. */
  hasError: boolean;
  setProject: (project: Project) => void;
}

const TenantContext = createContext<TenantContextValue | undefined>(undefined);

/**
 * Phase 7.8: there is deliberately no `organizations`/`setOrganization` on
 * this context. `core.auth.service._issue_access_token` bakes exactly one
 * `organization_id` into the access token's own claims at login -- every
 * request is scoped to that org for the lifetime of the session, and
 * `core.users.service.resolve_organization_for_login`'s own docstring
 * confirms password-auth accounts aren't even modeled as belonging to more
 * than one organization today. Nothing server-side would honor a
 * client-side "active organization" change, so exposing a setter here would
 * silently desync the UI from the session's real, immutable tenant --
 * `GET /organizations` itself only ever returns a single-element list
 * (see `app/api/routers/tenancy.py`'s own module docstring for why). A real
 * switcher needs a backend that can re-scope a session's `organization_id`
 * (or issue a token naming a different one the same user holds a role in),
 * which does not exist yet; build this once it does, not before.
 */
export function TenantProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading: isAuthLoading } = useAuth();
  const { toast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [organization, setOrganization] = useState<Organization | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  useEffect(() => {
    // Wait for AuthProvider to resolve (and set the access token) first --
    // fetching tenancy data before authentication settles sends requests
    // with no Authorization header at all, since it's a sibling/parent
    // provider whose own effect hasn't necessarily run yet.
    if (isAuthLoading) return;
    if (!isAuthenticated) {
      setIsLoading(false);
      return;
    }
    setHasError(false);
    listOrganizations()
      .then((orgs) => setOrganization(orgs[0] ?? null))
      .catch(() => {
        setHasError(true);
        toast({ variant: "error", title: "Couldn't load your organization", description: "Please refresh to try again." });
      })
      .finally(() => setIsLoading(false));
  }, [isAuthenticated, isAuthLoading, toast]);

  useEffect(() => {
    if (!organization) {
      setProjects([]);
      setProject(null);
      return;
    }
    listProjects(organization.id)
      .then((orgProjects) => {
        setProjects(orgProjects);
        setProject(orgProjects[0] ?? null);
      })
      .catch(() => {
        setHasError(true);
        toast({ variant: "error", title: "Couldn't load projects", description: "Please refresh to try again." });
      });
  }, [organization, toast]);

  const value = useMemo<TenantContextValue>(
    () => ({
      organization,
      project,
      projects,
      isLoading,
      hasError,
      setProject,
    }),
    [organization, project, projects, isLoading, hasError],
  );

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- context files exporting both the Provider and its hook is the standard pattern here
export function useTenant(): TenantContextValue {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be used within a TenantProvider");
  return ctx;
}
