/** The time windows the Observability page offers. They map to the endpoints' real `since` parameter. */
export type TimeWindow = "all" | "24h" | "7d" | "30d";

export const TIME_WINDOWS: Array<{ value: TimeWindow; label: string }> = [
  { value: "all", label: "All time" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
];

const WINDOW_MS: Record<Exclude<TimeWindow, "all">, number> = {
  "24h": 24 * 3600 * 1000,
  "7d": 7 * 24 * 3600 * 1000,
  "30d": 30 * 24 * 3600 * 1000,
};

export function isTimeWindow(value: string | null): value is TimeWindow {
  return value === "all" || value === "24h" || value === "7d" || value === "30d";
}

/** ISO timestamp for the endpoints' `since` parameter, or undefined for "all time". */
export function sinceFor(window: TimeWindow, now: number = Date.now()): string | undefined {
  return window === "all" ? undefined : new Date(now - WINDOW_MS[window]).toISOString();
}

/** "never" / "5s" / "3m 20s" / "2h 5m" for the age of the oldest queued job. */
export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export const OBSERVABILITY_TABS = ["agents", "mcp", "ingestion", "storage"] as const;
export type ObservabilityTab = (typeof OBSERVABILITY_TABS)[number];

export function isObservabilityTab(value: string | null): value is ObservabilityTab {
  return value === "agents" || value === "mcp" || value === "ingestion" || value === "storage";
}

/** Human-readable size (binary units). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}
