import { ArrowRight, FlaskConical } from "lucide-react";

import type { DemoOperatorOut } from "../../api/generated/model";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { roleTone } from "../console/roles";

interface DemoPickerProps {
  operators: readonly DemoOperatorOut[];
  /** Operator id being signed in, if any. */
  pendingId: string | null;
  disabled: boolean;
  onPick: (operator: DemoOperatorOut) => void;
}

/** "Ana Souza" -> "AS"; one word gives one letter. */
function initials(displayName: string): string {
  return displayName
    .split(/\s+/)
    .filter((word) => word !== "")
    .slice(0, 2)
    .map((word) => word.charAt(0).toUpperCase())
    .join("");
}

/** Evaluation environments only: one compact card per seeded operator, with what each can do in which project. */
export function DemoPicker({ operators, pendingId, disabled, onPick }: DemoPickerProps) {
  return (
    <>
      <p className="mt-4 flex items-start gap-2 rounded-sm border border-warning bg-warning-soft px-3 py-2 text-sm text-warning">
        <FlaskConical className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>Demo mode - choose a persona; no password needed. This is an evaluation environment.</span>
      </p>
      <ul className="mt-4 flex flex-col gap-2" aria-label="Demo operators">
        {operators.map((operator) => {
          const pending = pendingId === operator.id;
          return (
            <li key={operator.id} className="rounded-md border border-line bg-surface px-3 py-2.5 shadow-raised">
              <div className="flex items-center gap-3">
                <span
                  aria-hidden="true"
                  className="grid size-9 shrink-0 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary"
                >
                  {initials(operator.display_name)}
                </span>
                <div className="min-w-0 flex-1">
                  <h2 className="truncate text-sm font-semibold">{operator.display_name}</h2>
                  <p className="truncate font-mono text-xs text-ink-muted" title={operator.email}>
                    {operator.email}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  className="shrink-0"
                  disabled={disabled}
                  aria-label={pending ? undefined : `Continue as ${operator.display_name}`}
                  onClick={() => onPick(operator)}
                >
                  {pending ? "Signing in…" : "Continue"}
                  {pending ? null : <ArrowRight aria-hidden="true" />}
                </Button>
              </div>
              {operator.memberships.length > 0 ? (
                <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1" aria-label={`Projects of ${operator.display_name}`}>
                  {operator.memberships.map((membership) => (
                    <li key={membership.project_id} className="flex min-w-0 items-center gap-1.5 text-xs">
                      <span className="max-w-40 truncate text-ink-muted">{membership.project_name}</span>
                      <Badge tone={roleTone(membership.role)}>{membership.role}</Badge>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-ink-muted">Not a member of any project yet.</p>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}
