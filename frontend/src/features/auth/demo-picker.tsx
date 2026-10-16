import { FlaskConical, UserRound } from "lucide-react";

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

/** Evaluation environments only: one card per seeded operator, with what each can do in which project. */
export function DemoPicker({ operators, pendingId, disabled, onPick }: DemoPickerProps) {
  return (
    <>
      <p
        data-testid="demo-banner"
        className="mt-4 flex items-start gap-2 rounded-sm border border-warning bg-warning-soft p-3 text-sm text-warning"
      >
        <FlaskConical className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>Demo mode - choose a persona; no password needed. This is an evaluation environment.</span>
      </p>
      <ul className="mt-5 flex flex-col gap-3" aria-label="Demo operators">
        {operators.map((operator) => (
          <li key={operator.id} className="rounded-md border border-line bg-surface p-4 shadow-raised">
            <div className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className="grid size-9 shrink-0 place-items-center rounded-full bg-primary-soft text-primary"
              >
                <UserRound className="size-4" />
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="truncate text-sm font-semibold">{operator.display_name}</h2>
                <p className="truncate font-mono text-xs text-ink-muted">{operator.email}</p>
              </div>
            </div>
            {operator.memberships.length > 0 ? (
              <ul className="mt-3 flex flex-col gap-1.5" aria-label={`Projects of ${operator.display_name}`}>
                {operator.memberships.map((membership) => (
                  <li key={membership.project_id} className="flex items-center justify-between gap-3 text-sm">
                    <span className="min-w-0 truncate text-ink-muted">{membership.project_name}</span>
                    <Badge tone={roleTone(membership.role)}>{membership.role}</Badge>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-3 text-sm text-ink-muted">Not a member of any project yet.</p>
            )}
            <Button
              className="mt-4 w-full"
              variant="secondary"
              disabled={disabled}
              onClick={() => onPick(operator)}
            >
              {pendingId === operator.id ? "Signing in…" : `Continue as ${operator.display_name}`}
            </Button>
          </li>
        ))}
      </ul>
    </>
  );
}
