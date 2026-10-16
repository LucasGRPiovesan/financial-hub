import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Badge } from "../../components/ui/badge";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { deliveryTone } from "./event-types";

/** The humanized type ("Cobrança criada") with the raw type in mono beside it; unknown types show only the raw one. */
export function EventTypeLabel({ type }: { type: string }) {
  const { tLoose } = useI18n();
  const label = tLoose("events.types", type, "");
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-2">
      {label ? <span className="font-medium">{label}</span> : null}
      <code className="font-mono text-xs text-ink-muted">{type}</code>
    </span>
  );
}

export function DeliveryStatusBadge({ status }: { status: string }) {
  const { tLoose } = useI18n();
  return (
    <Badge tone={deliveryTone(status)} data-status={status}>
      {tLoose("events.detail.status", status)}
    </Badge>
  );
}

/** A copy-to-clipboard icon button; a clipboard failure is reported, never silent. */
export function CopyButton({ text, label, className }: { text: string; label: string; className?: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast({ tone: "danger", title: t("events.copyFailed") });
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={t("events.copyNamed", { label })}
        className={
          className ?? "grid size-6 shrink-0 place-items-center rounded-xs text-ink-faint hover:bg-surface-sunken hover:text-ink"
        }
      >
        {copied ? <Check className="size-3.5" aria-hidden="true" /> : <Copy className="size-3.5" aria-hidden="true" />}
      </button>
      <span className="sr-only" aria-live="polite">
        {copied ? t("events.copied") : ""}
      </span>
    </>
  );
}

/** An id in mono, truncated in lists, with a copy button. */
export function CopyableValue({ value, label, truncate = true }: { value: string; label: string; truncate?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={truncate ? "max-w-[10rem] truncate font-mono text-xs" : "font-mono text-xs break-all"} title={value}>
        {value}
      </span>
      <CopyButton text={value} label={label} />
    </span>
  );
}

/** Pretty-printed JSON in a scrollable, focusable block with a copy button. */
export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  const text = JSON.stringify(value, null, 2);
  return (
    <div className="relative rounded-md border border-line bg-surface-sunken">
      <div className="absolute top-2 right-2">
        <CopyButton text={text} label={label} className="grid size-7 place-items-center rounded-xs border border-line bg-surface text-ink-muted hover:text-ink" />
      </div>
      <pre
        tabIndex={0}
        aria-label={label}
        className="max-h-[28rem] overflow-auto p-4 pr-12 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words"
      >
        {text}
      </pre>
    </div>
  );
}
