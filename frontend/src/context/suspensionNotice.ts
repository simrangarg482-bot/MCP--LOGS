import type { SuspensionNotice } from "@/types/platform";

/**
 * What the "your organization is suspended" page shows. Only the organization's name and the operator's
 * customer-facing message are ever stored (the backend sends nothing else). sessionStorage so a reload of
 * /suspended still explains itself; it is not a credential and never drives an access decision.
 */
const KEY = "ekip.suspension_notice";
export const SUSPENDED_EVENT = "ekip:organization-suspended";

let memory: SuspensionNotice | null = null;

export function setSuspensionNotice(notice: SuspensionNotice): void {
  memory = notice;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(notice));
  } catch {
    // storage unavailable: the in-memory copy is enough for this page view
  }
  window.dispatchEvent(new Event(SUSPENDED_EVENT));
}

export function getSuspensionNotice(): SuspensionNotice | null {
  if (memory) return memory;
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<SuspensionNotice>;
      memory = {
        organizationName: typeof parsed.organizationName === "string" ? parsed.organizationName : null,
        customerMessage: typeof parsed.customerMessage === "string" ? parsed.customerMessage : null,
      };
    }
  } catch {
    memory = null;
  }
  return memory;
}

export function clearSuspensionNotice(): void {
  memory = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
