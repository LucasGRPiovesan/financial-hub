import { cva } from "class-variance-authority";
import { CircleCheck, CircleDashed, FlaskConical, Theater } from "lucide-react";

import { useT } from "../i18n";
import { cn } from "../lib/cn";
import type { Scope } from "../lib/scope";

/*
 * Each scope differs in border style as well as colour, so the label never depends on colour alone:
 * solid = real, dashed = provider test mode, double = local imitation, dotted = not built.
 */
const scopeBadge = cva(
  "inline-flex items-center gap-1 rounded-xs border px-1.5 py-px text-xs font-medium leading-4 whitespace-nowrap",
  {
    variants: {
      scope: {
        implemented: "border-solid border-scope-implemented bg-scope-implemented-soft text-scope-implemented",
        sandbox: "border-dashed border-scope-sandbox bg-scope-sandbox-soft text-scope-sandbox",
        simulated: "border-[3px] border-double border-scope-simulated bg-scope-simulated-soft text-scope-simulated",
        planned: "border-dotted border-scope-planned bg-scope-planned-soft text-scope-planned",
      },
    },
  },
);

const ICONS = {
  implemented: CircleCheck,
  sandbox: FlaskConical,
  simulated: Theater,
  planned: CircleDashed,
} as const;

interface ScopeBadgeProps {
  scope: Scope;
  className?: string;
}

export function ScopeBadge({ scope, className }: ScopeBadgeProps) {
  const Icon = ICONS[scope];
  const t = useT();
  return (
    <span className={cn(scopeBadge({ scope }), className)} data-scope={scope} title={t(`scope.descriptions.${scope}`)}>
      <Icon className="size-3 shrink-0" aria-hidden="true" />
      {t(`scope.${scope}`)}
    </span>
  );
}
