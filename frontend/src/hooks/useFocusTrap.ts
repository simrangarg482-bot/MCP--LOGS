import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/**
 * Minimal, dependency-free focus trap for dialogs/drawers/off-canvas panels.
 * While `active` is true it:
 *  - remembers the element that had focus before opening,
 *  - moves focus into the container (first focusable element, or the
 *    container itself if nothing inside is focusable),
 *  - keeps Tab/Shift+Tab cycling within the container,
 *  - restores focus to the previously focused element once deactivated.
 *
 * Follows the same small useEffect-based pattern as useClickOutside/
 * useMediaQuery rather than pulling in a focus-trap library.
 */
export function useFocusTrap(containerRef: RefObject<HTMLElement>, active: boolean): void {
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;

    previouslyFocused.current = document.activeElement as HTMLElement | null;

    const getFocusable = () => {
      const container = containerRef.current;
      if (!container) return [];
      return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    };

    const [first] = getFocusable();
    (first ?? containerRef.current)?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Tab") return;
      const elements = getFocusable();
      if (elements.length === 0) {
        event.preventDefault();
        return;
      }
      const firstEl = elements[0];
      const lastEl = elements[elements.length - 1];
      const activeEl = document.activeElement;

      if (event.shiftKey && activeEl === firstEl) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && activeEl === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previouslyFocused.current?.focus?.();
    };
    // `containerRef` is a ref object (stable identity) and intentionally
    // excluded from deps -- this effect should only re-run when `active`
    // toggles, not on every render of the consuming component.
  }, [active]);
}
