import type { LucideIcon } from "lucide-react";
import {
  MessageCircleQuestion,
  LayoutDashboard,
  AlertCircle,
  BookOpen,
  Lightbulb,
  Search,
  Plug,
  Bot,
  Wrench,
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
  { label: "Knowledge Gaps", path: "/knowledge-gaps", icon: Lightbulb, permission: "knowledge:review" },
  { label: "Search", path: "/search", icon: Search },
  { label: "Connectors", path: "/connectors", icon: Plug, permission: "tenancy:manage" },
  { label: "Agents", path: "/agents", icon: Bot, permission: "observability:read" },
  { label: "MCP Tools", path: "/mcp", icon: Wrench, permission: "observability:read" },
  { label: "Audit Log", path: "/audit", icon: ScrollText, permission: "audit:read" },
];

export const SETTINGS_NAV: NavItem[] = [{ label: "Settings", path: "/settings", icon: Settings }];
