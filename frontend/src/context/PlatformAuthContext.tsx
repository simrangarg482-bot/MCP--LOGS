import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as platformApi from "@/api/platform";
import { PLATFORM_SESSION_FAILED_EVENT } from "@/api/platform";
import type { PlatformOperator } from "@/types/platform";
import { clearPlatformToken, platformTokenExpiry, setPlatformToken } from "./platformTokenStore";

export type PlatformAvailability = "checking" | "unavailable" | "available";

interface PlatformAuthValue {
  availability: PlatformAvailability;
  operator: PlatformOperator | null;
  /** Why the last session ended (shown on the sign-in page), or null. */
  endedReason: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const PlatformAuthContext = createContext<PlatformAuthValue | undefined>(undefined);

export function PlatformAuthProvider({ children }: { children: ReactNode }) {
  const [availability, setAvailability] = useState<PlatformAvailability>("checking");
  const [operator, setOperator] = useState<PlatformOperator | null>(null);
  const [endedReason, setEndedReason] = useState<string | null>(null);
  const expiryTimer = useRef<number | null>(null);

  const endSession = useCallback((reason: string | null) => {
    if (expiryTimer.current !== null) window.clearTimeout(expiryTimer.current);
    expiryTimer.current = null;
    clearPlatformToken();
    setOperator(null);
    setEndedReason(reason);
  }, []);

  // Is the platform surface switched on at all? (the server-side kill switch)
  useEffect(() => {
    let cancelled = false;
    platformApi
      .getPlatformAvailability()
      .then((available) => {
        if (!cancelled) setAvailability(available ? "available" : "unavailable");
      })
      .catch(() => {
        if (!cancelled) setAvailability("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The server ended the session (revoked operator, password change, expired token) or switched the
  // surface off: drop the in-memory token and say so.
  useEffect(() => {
    const onFailed = (event: Event) => {
      const code = (event as CustomEvent<string>).detail;
      if (code === "platform.unavailable") setAvailability("unavailable");
      endSession(
        code === "platform.unavailable"
          ? "Platform access is no longer available."
          : "Your platform session ended. Sign in again.",
      );
    };
    window.addEventListener(PLATFORM_SESSION_FAILED_EVENT, onFailed);
    return () => window.removeEventListener(PLATFORM_SESSION_FAILED_EVENT, onFailed);
  }, [endSession]);

  const login = useCallback(
    async (email: string, password: string) => {
      const session = await platformApi.platformLogin(email, password);
      setPlatformToken(session.accessToken);
      try {
        setOperator(await platformApi.getPlatformOperator());
      } catch (error) {
        clearPlatformToken();
        throw error;
      }
      setEndedReason(null);
      // No refresh token exists: when the short-lived token expires the operator signs in again.
      const exp = platformTokenExpiry(session.accessToken);
      const ms = exp ? exp * 1000 - Date.now() : session.expiresIn * 1000;
      expiryTimer.current = window.setTimeout(
        () => endSession("Your platform session expired. Sign in again."),
        Math.max(ms, 0),
      );
    },
    [endSession],
  );

  const logout = useCallback(() => endSession(null), [endSession]);

  const value = useMemo<PlatformAuthValue>(
    () => ({ availability, operator, endedReason, login, logout }),
    [availability, operator, endedReason, login, logout],
  );
  return <PlatformAuthContext.Provider value={value}>{children}</PlatformAuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- provider + hook in one file is this codebase's pattern
export function usePlatformAuth(): PlatformAuthValue {
  const ctx = useContext(PlatformAuthContext);
  if (!ctx) throw new Error("usePlatformAuth must be used within a PlatformAuthProvider");
  return ctx;
}
