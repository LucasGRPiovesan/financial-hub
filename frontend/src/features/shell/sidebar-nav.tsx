import { Link } from "@tanstack/react-router";
import { Fragment } from "react";

import { cn } from "../../lib/cn";
import { NAV_GROUPS } from "./nav";

export function BrandMark({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <span
        aria-hidden="true"
        className="grid size-8 place-items-center rounded-sm bg-chrome-raised font-mono text-sm font-semibold text-chrome-brass ring-1 ring-chrome-line"
      >
        FH
      </span>
      <span className="text-base font-semibold tracking-tight text-chrome-ink">Financial Hub</span>
    </div>
  );
}

interface SidebarNavProps {
  /** Called after a link is chosen (closes the mobile sheet). */
  onNavigate?: () => void;
}

export function SidebarNav({ onNavigate }: SidebarNavProps) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-1 overflow-y-auto px-3 pb-6">
      {NAV_GROUPS.map((group, index) => (
        <Fragment key={group[0]?.to ?? index}>
          {index > 0 ? <div aria-hidden="true" className="mx-2 my-2 h-px bg-chrome-line" /> : null}
          <ul className="flex flex-col gap-0.5">
            {group.map((item) => (
              <li key={item.to}>
                <Link
                  to={item.to}
                  activeOptions={{ exact: item.to === "/" }}
                  onClick={onNavigate}
                  className={cn(
                    "group relative flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 text-sm font-medium text-chrome-muted transition-colors hover:bg-chrome-raised hover:text-chrome-ink",
                    "data-[status=active]:bg-chrome-raised data-[status=active]:text-chrome-ink",
                    "before:absolute before:inset-y-1.5 before:-left-3 before:w-0.5 before:rounded-full before:bg-transparent data-[status=active]:before:bg-chrome-brass",
                  )}
                >
                  <item.icon className="size-4 shrink-0" aria-hidden="true" />
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </Fragment>
      ))}
    </nav>
  );
}
