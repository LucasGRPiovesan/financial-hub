import type { InputHTMLAttributes, Ref } from "react";

import { cn } from "../../lib/cn";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  ref?: Ref<HTMLInputElement>;
}

export function Input({ className, type = "text", ref, ...props }: InputProps) {
  return (
    <input
      ref={ref}
      type={type}
      className={cn(
        "motion-ui h-10 w-full rounded-sm border border-control bg-surface px-3 text-base text-ink shadow-raised placeholder:text-ink-faint disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger",
        className,
      )}
      {...props}
    />
  );
}
