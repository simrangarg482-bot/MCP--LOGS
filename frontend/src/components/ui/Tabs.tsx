import { useRef } from "react";
import { cn } from "@/utils/cn";

interface TabItem {
  key: string;
  label: string;
  count?: number;
}

interface TabsProps {
  items: TabItem[];
  activeKey: string;
  onChange: (key: string) => void;
  /** Distinguishes this tablist's tab/panel ids from any other `Tabs`
   * instance on the same page -- pass a page- or feature-specific value
   * (e.g. "incident-detail") when a page renders more than one `Tabs`. */
  idPrefix?: string;
}

function tabPanelId(idPrefix: string, key: string): string {
  return `${idPrefix}-tabpanel-${key}`;
}

function tabId(idPrefix: string, key: string): string {
  return `${idPrefix}-tab-${key}`;
}

export function Tabs({ items, activeKey, onChange, idPrefix = "tabs" }: TabsProps) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  function focusAndActivate(index: number) {
    const count = items.length;
    const next = items[((index % count) + count) % count];
    tabRefs.current[((index % count) + count) % count]?.focus();
    onChange(next.key);
  }

  function handleKeyDown(event: React.KeyboardEvent, index: number) {
    switch (event.key) {
      case "ArrowRight":
        event.preventDefault();
        focusAndActivate(index + 1);
        break;
      case "ArrowLeft":
        event.preventDefault();
        focusAndActivate(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focusAndActivate(0);
        break;
      case "End":
        event.preventDefault();
        focusAndActivate(items.length - 1);
        break;
    }
  }

  return (
    <div role="tablist" className="flex items-center gap-1 overflow-x-auto border-b border-border scrollbar-thin">
      {items.map((item, index) => {
        const isActive = item.key === activeKey;
        return (
          <button
            key={item.key}
            ref={(el) => {
              tabRefs.current[index] = el;
            }}
            id={tabId(idPrefix, item.key)}
            role="tab"
            aria-selected={isActive}
            aria-controls={tabPanelId(idPrefix, item.key)}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(item.key)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              "relative flex shrink-0 items-center gap-1.5 whitespace-nowrap px-3 py-2.5 text-sm font-medium transition-colors",
              isActive ? "text-ink" : "text-ink-muted hover:text-ink",
            )}
          >
            {item.label}
            {item.count !== undefined && (
              <span className="rounded-full bg-slate-100 px-1.5 text-xs text-ink-muted">{item.count}</span>
            )}
            {isActive && <span className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-accent" />}
          </button>
        );
      })}
    </div>
  );
}

/** Wraps one tab's content with the `role="tabpanel"`/`aria-labelledby`/`id`
 * triple `Tabs` above expects a matching panel to have -- both current
 * consumers (`AskPage`, `IncidentDetailPage`) conditionally render only the
 * active panel's content rather than keeping every panel mounted, so this
 * only ever renders the one currently-selected panel, not `hidden`-toggled
 * siblings. */
export function TabPanel({
  idPrefix,
  tabKey,
  className,
  children,
}: {
  idPrefix: string;
  tabKey: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      id={tabPanelId(idPrefix, tabKey)}
      role="tabpanel"
      aria-labelledby={tabId(idPrefix, tabKey)}
      tabIndex={0}
      className={className}
    >
      {children}
    </div>
  );
}
