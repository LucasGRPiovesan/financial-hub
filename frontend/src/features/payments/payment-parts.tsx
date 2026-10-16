import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Badge } from "../../components/ui/badge";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { methodKey, statusKey, statusTone } from "./payment-status";

/** The primary, derived status. The text is always shown, so colour is never the only signal. */
export function StatusBadge({ status }: { status: string }) {
  const { tLoose } = useI18n();
  return (
    <Badge tone={statusTone(status)} data-status={status}>
      {tLoose("payments.status", statusKey(status), status)}
    </Badge>
  );
}

export function MethodBadge({ method }: { method: string }) {
  const { tLoose } = useI18n();
  return <Badge tone="neutral">{tLoose("payments.method", methodKey(method), method)}</Badge>;
}

/** The payment id in mono with a copy button (a clipboard failure is reported, never silent). */
export function CopyableId({ id, truncate = true }: { id: string; truncate?: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast({ tone: "danger", title: t("payments.copyFailed") });
    }
  }

  return (
    <span className="inline-flex items-center gap-1">
      <span className={truncate ? "max-w-[9.5rem] truncate font-mono text-xs" : "font-mono text-xs break-all"} title={id}>
        {id}
      </span>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={t("payments.copyId", { id })}
        className="grid size-6 shrink-0 place-items-center rounded-xs text-ink-faint hover:bg-surface-sunken hover:text-ink"
      >
        {copied ? <Check className="size-3.5" aria-hidden="true" /> : <Copy className="size-3.5" aria-hidden="true" />}
      </button>
      <span className="sr-only" aria-live="polite">
        {copied ? t("payments.copied") : ""}
      </span>
    </span>
  );
}
