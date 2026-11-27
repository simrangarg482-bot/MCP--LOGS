import type { LucideIcon } from "lucide-react";
import {
  MessageCircleQuestion,
  Compass,
  LayoutDashboard,
  AlertCircle,
  BookOpen,
  BookOpenCheck,
  Lightbulb,
  Search,
  Plug,
  Activity,
  ScrollText,
  Settings,
} from "lucide-react";

export interface NavItem {
  label: string;
  path: string;
  icon: LucideIcon;
  // A permission code required to see this item at all, matching the real
  // backend gate its page's primary data call now enforces (see
  // Phase 1's tenancy:manage/knowledge:review fixes). Omit for pages with
  // no such gate. UX only -- the backend re-checks regardless.
  permission?: string;
}

export const PRIMARY_NAV: NavItem[] = [
  { label: "Ask EKIP", path: "/ask", icon: MessageCircleQuestion },
  { label: "Dashboard", path: "/dashboard", icon: LayoutDashboard },
  { label: "Incidents", path: "/incidents", icon: AlertCircle },
  { label: "Knowledge", path: "/knowledge", icon: BookOpen },
  { label: "Knowledge Review", path: "/knowledge/review", icon: BookOpenCheck, permission: "knowledge:review" },
  { label: "Knowledge Gaps", path: "/knowledge-gaps", icon: Lightbulb, permission: "knowledge:review" },
  { label: "Search", path: "/search", icon: Search },
  { label: "Connectors", path: "/connectors", icon: Plug, permission: "tenancy:manage" },
  // One entry for agents, MCP tools and ingestion (tabs on the page). The old /agents and /mcp URLs
  // redirect to the matching tab, so existing links and bookmarks keep working.
  { label: "Observability", path: "/observability", icon: Activity, permission: "observability:read" },
  { label: "Audit Log", path: "/audit", icon: ScrollText, permission: "audit:read" },
  { label: "Overview", path: "/about", icon: Compass },
];

export const SETTINGS_NAV: NavItem[] = [{ label: "Settings", path: "/settings", icon: Settings }];
