import type { ComponentType, HTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/cn";

interface EmptyStateProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  icon?: ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" }>;
  title: string;
  description?: ReactNode;
  /** Badge or other status shown under the title (e.g. a ScopeBadge). */
  status?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ icon: Icon, title, description, status, action, className, ...props }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-md border border-dashed border-line-strong bg-surface px-6 py-14 text-center",
        className,
      )}
      {...props}
    >
      {Icon ? (
        <span className="grid size-11 place-items-center rounded-md bg-surface-sunken text-ink-muted ring-1 ring-line">
          <Icon className="size-5" aria-hidden="true" />
        </span>
      ) : null}
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {status}
      {description ? <p className="max-w-prose text-sm text-ink-muted">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}
