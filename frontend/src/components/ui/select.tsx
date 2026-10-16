import { ChevronDown } from "lucide-react";
import type { Ref, SelectHTMLAttributes } from "react";

import { cn } from "../../lib/cn";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  ref?: Ref<HTMLSelectElement>;
}

/** A native select (keyboard, mobile pickers and screen readers for free) dressed like Input. */
export function Select({ className, children, ref, ...props }: SelectProps) {
  return (
    <span className="relative block w-full">
      <select
        ref={ref}
        className={cn(
          "h-10 w-full appearance-none rounded-sm border border-control bg-surface pr-9 pl-3 text-base text-ink shadow-raised disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 text-ink-faint"
        aria-hidden="true"
      />
    </span>
  );
}
