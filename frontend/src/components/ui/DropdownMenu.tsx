import { cloneElement, isValidElement, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { useClickOutside } from "@/hooks/useClickOutside";
import { cn } from "@/utils/cn";

interface DropdownItem {
  label: string;
  onSelect: () => void;
  destructive?: boolean;
}

interface DropdownMenuProps {
  trigger: ReactNode;
  items: DropdownItem[];
  align?: "left" | "right";
}

export function DropdownMenu({ trigger, items, align = "right" }: DropdownMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();

  useClickOutside(ref, () => setOpen(false));

  useEffect(() => {
    if (open) itemRefs.current[0]?.focus();
  }, [open]);

  function close(restoreFocus: boolean) {
    setOpen(false);
    if (restoreFocus) ref.current?.querySelector<HTMLElement>("[aria-haspopup]")?.focus();
  }

  function focusItem(index: number) {
    const count = items.length;
    itemRefs.current[((index % count) + count) % count]?.focus();
  }

  function handleMenuKeyDown(event: React.KeyboardEvent, index: number) {
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        close(true);
        break;
      case "ArrowDown":
        event.preventDefault();
        focusItem(index + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focusItem(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focusItem(0);
        break;
      case "End":
        event.preventDefault();
        focusItem(items.length - 1);
        break;
    }
  }

  const triggerNode = isValidElement(trigger)
    ? cloneElement(trigger as ReactElement<Record<string, unknown>>, {
        "aria-haspopup": "menu",
        "aria-expanded": open,
        "aria-controls": open ? menuId : undefined,
      })
    : trigger;

  return (
    <div className="relative inline-block" ref={ref}>
      <div
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(event) => {
          if (!open && (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {triggerNode}
      </div>
      {open && (
        <div
          id={menuId}
          role="menu"
          className={cn(
            "absolute z-40 mt-1 min-w-[10rem] rounded-md border border-border bg-white py-1 shadow-panel",
            align === "right" ? "right-0" : "left-0",
          )}
        >
          {items.map((item, index) => (
            <button
              key={item.label}
              ref={(el) => {
                itemRefs.current[index] = el;
              }}
              role="menuitem"
              tabIndex={-1}
              onClick={() => {
                item.onSelect();
                close(false);
              }}
              onKeyDown={(event) => handleMenuKeyDown(event, index)}
              className={cn(
                "block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-50 focus-visible:bg-slate-50 focus-visible:outline-none",
                item.destructive ? "text-critical" : "text-ink",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
