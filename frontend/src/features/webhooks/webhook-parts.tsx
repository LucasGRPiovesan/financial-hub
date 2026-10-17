import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useI18n } from "../../i18n";
import { cn } from "../../lib/cn";
import { EVENT_TYPE_OPTIONS } from "../events/event-types";

import { ALL_EVENTS, type EventSelection } from "./selection";

/** Chips for an endpoint's event-type filter; `*` (or an empty list) reads as "all events". */
export function EventFilterChips({ eventTypes, max = 3 }: { eventTypes: readonly string[]; max?: number }) {
  const { t } = useI18n();
  if (eventTypes.length === 0 || eventTypes.includes(ALL_EVENTS)) {
    return <Badge tone="neutral">{t("webhooks.allEvents")}</Badge>;
  }
  const shown = eventTypes.slice(0, max);
  const hidden = eventTypes.length - shown.length;
  return (
    <ul className="flex flex-wrap gap-1" aria-label={t("webhooks.cols.events")}>
      {shown.map((type) => (
        <li key={type}>
          <Badge tone="neutral" className="font-mono">
            {type}
          </Badge>
        </li>
      ))}
      {hidden > 0 ? (
        <li>
          <Badge tone="neutral" title={eventTypes.slice(max).join(", ")}>
            {t("webhooks.moreEvents", { count: hidden })}
          </Badge>
        </li>
      ) : null}
    </ul>
  );
}

export function EndpointStatusBadge({ enabled }: { enabled: boolean }) {
  const { t } = useI18n();
  return <Badge tone={enabled ? "success" : "neutral"}>{enabled ? t("webhooks.enabled") : t("webhooks.disabled")}</Badge>;
}

const OUTCOME_TONE = { delivered: "success", failed: "danger", refused: "warning" } as const;

export function AttemptOutcomeBadge({ outcome }: { outcome: string }) {
  const { tLoose } = useI18n();
  const tone = OUTCOME_TONE[outcome as keyof typeof OUTCOME_TONE] ?? "neutral";
  return (
    <Badge tone={tone} data-outcome={outcome}>
      {tLoose("webhooks.attempts.outcome", outcome)}
    </Badge>
  );
}

/** "All events" or a multi-select of the event catalog. Filters already on the endpoint that the catalog lacks stay listed. */
export function EventTypesPicker({
  value,
  onChange,
  error,
}: {
  value: EventSelection;
  onChange: (next: EventSelection) => void;
  error?: string | undefined;
}) {
  const { t } = useI18n();
  const options = [...EVENT_TYPE_OPTIONS, ...value.types.filter((type) => !EVENT_TYPE_OPTIONS.includes(type))];
  const errorId = "webhook-event-types-error";

  function toggle(type: string, checked: boolean) {
    onChange({ all: false, types: checked ? [...value.types, type] : value.types.filter((item) => item !== type) });
  }

  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={error ? errorId : undefined}>
      <legend className="mb-1 text-sm font-medium text-ink">{t("webhooks.form.eventsLabel")}</legend>
      <p className="text-xs text-ink-muted">{t("webhooks.form.eventsHint")}</p>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={value.all} onChange={(event) => onChange({ all: event.target.checked, types: [] })} />
        {t("webhooks.form.allEventsOption")}
      </label>
      <div className={cn("grid gap-x-4 gap-y-1.5 sm:grid-cols-2", value.all && "opacity-50")}>
        {options.map((type) => (
          <label key={type} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!value.all && value.types.includes(type)} onChange={(event) => toggle(type, event.target.checked)} />
            <code className="font-mono text-xs">{type}</code>
          </label>
        ))}
      </div>
      {error ? (
        <p id={errorId} className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

/**
 * The one-time secret panel: mono value, copy button, warning and the Standard Webhooks hint.
 * The plaintext is a prop of this component only; the caller drops it when `onDone` runs.
 */
export function SecretPanel({ secret, description, warn, onDone }: { secret: string; description: string; warn: boolean; onDone: () => void }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState<"yes" | "failed" | null>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied("yes");
    } catch {
      setCopied("failed");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{t("webhooks.secret.title")}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <div className="flex gap-2">
        <Input readOnly aria-label={t("webhooks.secret.label")} value={secret} className="font-mono text-xs" onFocus={(event) => event.currentTarget.select()} />
        <Button type="button" variant="secondary" onClick={() => void copy()}>
          {copied === "yes" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied === "yes" ? t("apiKeys.copied") : t("apiKeys.copy")}
          <span className="sr-only"> {t("webhooks.secret.copy")}</span>
        </Button>
      </div>
      {copied === "failed" ? (
        <p role="status" className="text-sm text-ink-muted">
          {t("apiKeys.clipboardFailed")}
        </p>
      ) : null}
      <p className={cn("rounded-md border px-3 py-2 text-sm", warn ? "border-danger text-danger" : "border-line bg-surface-sunken text-ink")} role={warn ? "alert" : undefined}>
        {t("webhooks.secret.warning")}
      </p>
      <div className="rounded-md border border-line px-3 py-2">
        <h3 className="text-sm font-medium">{t("webhooks.secret.verifyTitle")}</h3>
        <p className="mt-1 text-xs text-ink-muted">{t("webhooks.secret.verifyHint")}</p>
      </div>
      <DialogFooter>
        <Button type="button" onClick={onDone}>
          {t("webhooks.secret.done")}
        </Button>
      </DialogFooter>
    </div>
  );
}
