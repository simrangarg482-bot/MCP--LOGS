import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppLayout } from "@/layouts/AppLayout";
import { AuthLayout } from "@/layouts/AuthLayout";
import { ProtectedRoute } from "./ProtectedRoute";
import { PlatformGate, PlatformRoot } from "@/layouts/PlatformLayout";
import { PlatformLoginPage } from "@/pages/platform/PlatformLoginPage";
import { PlatformOrganizationsPage } from "@/pages/platform/PlatformOrganizationsPage";
import { PlatformOrganizationDetailPage } from "@/pages/platform/PlatformOrganizationDetailPage";
import { SuspendedPage } from "@/pages/auth/SuspendedPage";

import { AcceptInvitationPage } from "@/pages/auth/AcceptInvitationPage";
import { ForgotPasswordPage } from "@/pages/auth/ForgotPasswordPage";
import { LoginPage } from "@/pages/auth/LoginPage";
import { ResetPasswordPage } from "@/pages/auth/ResetPasswordPage";
import { SignupPage } from "@/pages/auth/SignupPage";
import { SsoCallbackPage } from "@/pages/auth/SsoCallbackPage";
import { SsoLoginPage } from "@/pages/auth/SsoLoginPage";
import { VerifyEmailPage } from "@/pages/auth/VerifyEmailPage";
import { AboutPage } from "@/pages/about/AboutPage";
import { AskPage } from "@/pages/ask/AskPage";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { IncidentsListPage } from "@/pages/incidents/IncidentsListPage";
import { IncidentCreatePage } from "@/pages/incidents/IncidentCreatePage";
import { IncidentDetailPage } from "@/pages/incidents/IncidentDetailPage";
import { KnowledgeGapsPage } from "@/pages/knowledge-gaps/KnowledgeGapsPage";
import { KnowledgeListPage } from "@/pages/knowledge/KnowledgeListPage";
import { KnowledgeReviewPage } from "@/pages/knowledge/KnowledgeReviewPage";
import { KnowledgeDetailPage } from "@/pages/knowledge/KnowledgeDetailPage";
import { SearchPage } from "@/pages/search/SearchPage";
import { ConnectorsPage } from "@/pages/connectors/ConnectorsPage";
import { ObservabilityPage } from "@/pages/observability/ObservabilityPage";
import { AuditPage } from "@/pages/audit/AuditPage";
import { SettingsLayout } from "@/pages/settings/SettingsLayout";
import { OrganizationSettingsPage } from "@/pages/settings/OrganizationSettingsPage";
import { ProjectSettingsPage } from "@/pages/settings/ProjectSettingsPage";
import { UsersSettingsPage } from "@/pages/settings/UsersSettingsPage";
import { SsoSettingsPage } from "@/pages/settings/SsoSettingsPage";
import { ProvisioningSettingsPage } from "@/pages/settings/ProvisioningSettingsPage";
import { ConnectorsSettingsPage } from "@/pages/settings/ConnectorsSettingsPage";
import { AccessRulesSettingsPage } from "@/pages/settings/AccessRulesSettingsPage";
import { RolesSettingsPage } from "@/pages/settings/RolesSettingsPage";
import { MyAccessSettingsPage } from "@/pages/settings/MyAccessSettingsPage";
import { SecuritySettingsPage } from "@/pages/settings/SecuritySettingsPage";
import { NotFoundPage } from "@/pages/misc/NotFoundPage";

export const router = createBrowserRouter([
  // Public, unauthenticated. `/about` explains the product and is linked from
  // both the login screen and the in-app "Overview" nav item.
  { path: "/about", element: <AboutPage /> },
  // The platform-operator console: a separate surface with its own sign-in, token and layout. Nothing
  // here uses the tenant session, and the server authorizes every request on its own.
  {
    path: "/platform",
    element: <PlatformRoot />,
    children: [
      { path: "login", element: <PlatformLoginPage /> },
      {
        element: <PlatformGate />,
        children: [
          { index: true, element: <PlatformOrganizationsPage /> },
          { path: "organizations/:id", element: <PlatformOrganizationDetailPage /> },
        ],
      },
    ],
  },
  {
    element: <AuthLayout />,
    children: [
      { path: "/login", element: <LoginPage /> },
      { path: "/signup", element: <SignupPage /> },
      // The organization-suspended explanation. Public on purpose: a suspended session cannot call any
      // authenticated API, so this page calls none -- which is also what keeps it from redirect-looping.
      { path: "/suspended", element: <SuspendedPage /> },
      { path: "/forgot-password", element: <ForgotPasswordPage /> },
      { path: "/reset-password", element: <ResetPasswordPage /> },
      { path: "/o/:slug/login", element: <SsoLoginPage /> },
      { path: "/o/:slug/callback", element: <SsoCallbackPage /> },
      { path: "/verify-email", element: <VerifyEmailPage /> },
      { path: "/invitations/:invitationId/accept", element: <AcceptInvitationPage /> },
    ],
  },
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { path: "/", element: <Navigate to="/ask" replace /> },
          { path: "/ask", element: <AskPage /> },
          { path: "/dashboard", element: <DashboardPage /> },
          { path: "/incidents", element: <IncidentsListPage /> },
          { path: "/incidents/new", element: <IncidentCreatePage /> },
          { path: "/incidents/:id", element: <IncidentDetailPage /> },
          { path: "/knowledge", element: <KnowledgeListPage /> },
          { path: "/knowledge/review", element: <KnowledgeReviewPage /> },
          { path: "/knowledge/:id", element: <KnowledgeDetailPage /> },
          { path: "/knowledge-gaps", element: <KnowledgeGapsPage /> },
          { path: "/search", element: <SearchPage /> },
          { path: "/connectors", element: <ConnectorsPage /> },
          { path: "/observability", element: <ObservabilityPage /> },
          // The former standalone pages now live in Observability tabs; keep their URLs working.
          { path: "/agents", element: <Navigate to="/observability?tab=agents" replace /> },
          { path: "/mcp", element: <Navigate to="/observability?tab=mcp" replace /> },
          { path: "/audit", element: <AuditPage /> },
          {
            path: "/settings",
            element: <SettingsLayout />,
            children: [
              { index: true, element: <Navigate to="/settings/organization" replace /> },
              { path: "organization", element: <OrganizationSettingsPage /> },
              { path: "project", element: <ProjectSettingsPage /> },
              { path: "users", element: <UsersSettingsPage /> },
              { path: "sso", element: <SsoSettingsPage /> },
              { path: "provisioning", element: <ProvisioningSettingsPage /> },
              { path: "connectors", element: <ConnectorsSettingsPage /> },
              { path: "access-rules", element: <AccessRulesSettingsPage /> },
              { path: "roles", element: <RolesSettingsPage /> },
              { path: "my-access", element: <MyAccessSettingsPage /> },
              { path: "security", element: <SecuritySettingsPage /> },
            ],
          },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
]);
