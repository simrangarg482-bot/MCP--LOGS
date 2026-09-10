import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "@/utils/cn";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "h-9 w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 text-sm text-ink placeholder:text-ink-subtle",
        "transition-colors focus-visible:border-accent focus-visible:bg-white/[0.06]",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";
