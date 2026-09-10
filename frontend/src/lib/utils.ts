import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * shadcn/ui-standard class combiner. This is the canonical location
 * (`@/lib/utils`) that shadcn components import from; `@/utils/cn`
 * re-exports it for the modules that already used that path.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
