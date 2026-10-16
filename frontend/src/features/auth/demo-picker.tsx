import { ArrowRight, FlaskConical, LoaderCircle } from "lucide-react";

import type { DemoOperatorOut } from "../../api/generated/model";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { useI18n } from "../../i18n";
import { cn } from "../../lib/cn";
import { roleTone } from "../console/roles";

interface DemoPickerProps {
  operators: readonly DemoOperatorOut[];
  /** Operator id being signed in, if any. */
  pendingId: string | null;
  disabled: boolean;
  onPick: (operator: DemoOperatorOut) => void;
}

/** Project chips shown per operator; the rest collapse into a "+N" counter so one card never grows unbounded. */
const MAX_CHIPS = 3;

/** "Ana Souza" -> "AS"; one word gives one letter. */
function initials(displayName: string): string {
  return displayName
    .split(/\s+/)
    .filter((word) => word !== "")
    .slice(0, 2)
    .map((word) => word.charAt(0).toUpperCase())
    .join("");
}

/** Evaluation environments only: one card per seeded operator, with what each can do in which project. */
export function DemoPicker({ operators, pendingId, disabled, onPick }: DemoPickerProps) {
  const { t, tLoose } = useI18n();
  return (
    <>
      <p className="mt-5 flex items-start gap-2 rounded-sm border border-warning bg-warning-soft px-3 py-2 text-sm text-warning">
        <FlaskConical className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>{t("auth.demoBanner")}</span>
      </p>
      <ul className="stagger-in mt-4 flex flex-col gap-2.5" aria-label={t("auth.demoOperators")}>
        {operators.map((operator) => {
          const pending = pendingId === operator.id;
          return (
            <li
              key={operator.id}
              aria-busy={pending || undefined}
              className="motion-ui flex flex-col gap-3 rounded-md border border-line bg-surface p-3.5 shadow-raised hover:border-line-strong has-[button:focus-visible]:border-ring has-[button:focus-visible]:ring-1 has-[button:focus-visible]:ring-ring sm:flex-row sm:items-center"
            >
              <div className="flex min-w-0 flex-1 items-center gap-3">
                <span
                  aria-hidden="true"
                  className="grid size-10 shrink-0 place-items-center rounded-full bg-primary-soft text-sm font-semibold text-primary ring-1 ring-primary/15"
                >
                  {initials(operator.display_name)}
                </span>
                <div className="min-w-0 flex-1">
                  <h2 className="truncate text-sm font-semibold">{operator.display_name}</h2>
                  <p className="truncate font-mono text-xs text-ink-muted" title={operator.email}>
                    {operator.email}
                  </p>
                  {operator.memberships.length > 0 ? (
                    <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1" aria-label={t("auth.projectsOf", { name: operator.display_name })}>
                      {operator.memberships.slice(0, MAX_CHIPS).map((membership) => (
                        <li key={membership.project_id} className="flex min-w-0 max-w-full items-center gap-1.5 text-xs">
                          <span className="max-w-32 truncate text-ink-muted" title={membership.project_name}>
                            {membership.project_name}
                          </span>
                          <Badge tone={roleTone(membership.role)}>{tLoose("roles", membership.role)}</Badge>
                        </li>
                      ))}
                      {operator.memberships.length > MAX_CHIPS ? (
                        <li className="text-xs text-ink-muted">+{operator.memberships.length - MAX_CHIPS}</li>
                      ) : null}
                    </ul>
                  ) : (
                    <p className="mt-1.5 text-xs text-ink-muted">{t("auth.noProjects")}</p>
                  )}
                </div>
              </div>
              <Button
                size="md"
                className={cn("shrink-0 sm:min-w-28", pending && "disabled:opacity-100")}
                disabled={disabled}
                aria-label={pending ? undefined : t("auth.continueAs", { name: operator.display_name })}
                onClick={() => onPick(operator)}
              >
                {pending ? (
                  <>
                    <LoaderCircle className="motion-safe:animate-spin" aria-hidden="true" />
                    {t("auth.signingIn")}
                  </>
                ) : (
                  <>
                    {t("auth.signIn")}
                    <ArrowRight aria-hidden="true" />
                  </>
                )}
              </Button>
            </li>
          );
        })}
      </ul>
    </>
  );
}
