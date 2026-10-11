import type { ReactNode } from "react";

import type { Scope } from "../lib/scope";
import { ScopeBadge } from "./scope-badge";

interface PageHeaderProps {
  title: string;
  description?: string;
  scope?: Scope;
  actions?: ReactNode;
}

/** Page title over a double ledger rule: the console's one recurring flourish. */
export function PageHeader({ title, description, scope, actions }: PageHeaderProps) {
  return (
    <header className="ledger-rule mb-6 flex flex-wrap items-end justify-between gap-x-6 gap-y-3 pb-4">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {scope ? <ScopeBadge scope={scope} /> : null}
        </div>
        {description ? <p className="mt-1 max-w-prose text-sm text-ink-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}
