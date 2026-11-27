import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { LoadingState } from "@/components/ui/LoadingState";
import { getSuspensionNotice } from "@/context/suspensionNotice";

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <LoadingState label="Checking session…" />
      </div>
    );
  }

  if (isAuthenticated) return <Outlet />;
  // Signed out BECAUSE the organization was suspended: explain it instead of a bare sign-in form. The
  // /suspended page is outside this guard and calls no authenticated API, so this cannot loop.
  return <Navigate to={getSuspensionNotice() ? "/suspended" : "/login"} replace />;
}
