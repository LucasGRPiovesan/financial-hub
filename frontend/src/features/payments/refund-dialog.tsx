import { useState, type FormEvent } from "react";

import type { PaymentOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { formatMoney } from "../../lib/money";
import { errorText, fieldErrorsOf } from "../console/errors";
import { moneyOf } from "./payment-status";
import { newIdempotencyKey, useCreateRefund } from "./payments-api";
import { formatReaisInput, parseRefundAmount } from "./refund-amount";

const MAX_REASON = 500;

export function RefundDialog({ payment, open, onClose }: { payment: PaymentOut; open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>{open ? <RefundForm payment={payment} onClose={onClose} /> : null}</DialogContent>
    </Dialog>
  );
}

// Mounted only while open, so closing drops the typed values and the attempt's idempotency key.
function RefundForm({ payment, onClose }: { payment: PaymentOut; onClose: () => void }) {
  const { t, locale } = useI18n();
  const toast = useToast();
  const { refund, isPending } = useCreateRefund(payment.project_id, payment.id);
  const [mode, setMode] = useState<"full" | "partial">("full");
  const [amountText, setAmountText] = useState("");
  const [reason, setReason] = useState("");
  const [amountError, setAmountError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  // One key per attempt: resending the same request replays instead of refunding twice; editing it starts a new attempt.
  const [attemptKey, setAttemptKey] = useState(newIdempotencyKey);

  const refundable = payment.refundable_amount;
  const fmt = (centavos: number) => formatMoney(moneyOf(centavos, payment.currency), { locale });

  function edit(change: () => void) {
    change();
    setAttemptKey(newIdempotencyKey());
    setAmountError(null);
    setFormError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    let amount: number | null = null;
    if (mode === "partial") {
      const parsed = parseRefundAmount(amountText);
      if (!parsed.ok) {
        setAmountError(t(`payments.refund.errors.${parsed.error}`));
        return;
      }
      if (parsed.centavos > BigInt(refundable)) {
        setAmountError(t("payments.refund.errors.exceeds", { amount: fmt(refundable) }));
        return;
      }
      // Bounded by refundable_amount (a JSON number), so it is a safe integer.
      amount = Number(parsed.centavos);
    }
    if (reason.length > MAX_REASON) {
      setFormError(t("payments.refund.reasonTooLong"));
      return;
    }
    try {
      const done = await refund({ amount, reason: reason.trim() === "" ? null : reason.trim() }, attemptKey);
      toast({
        tone: "success",
        title: t("payments.refund.done"),
        description: t("payments.refund.doneDescription", { amount: fmt(done.amount), id: payment.id }),
      });
      onClose();
    } catch (error) {
      const mapped = fieldErrorsOf(error);
      const onAmount = mapped.fields.amount;
      if (onAmount) setAmountError(onAmount);
      const message = mapped.form ?? (onAmount ? null : errorText(error));
      setFormError(message);
      if (message) toast({ tone: "danger", title: t("payments.refund.failed"), description: message });
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("payments.refund.title")}</DialogTitle>
        <DialogDescription>{t("payments.refund.description", { amount: fmt(refundable) })}</DialogDescription>
      </DialogHeader>

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">{t("payments.refund.modeLabel")}</legend>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" name="refund-mode" checked={mode === "full"} onChange={() => edit(() => setMode("full"))} />
          {t("payments.refund.full", { amount: fmt(refundable) })}
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" name="refund-mode" checked={mode === "partial"} onChange={() => edit(() => setMode("partial"))} />
          {t("payments.refund.partial")}
        </label>
      </fieldset>

      {mode === "partial" ? (
        <Field label={t("payments.refund.amountLabel")} error={amountError ?? undefined} hint={t("payments.refund.amountHint")}>
          <Input
            value={amountText}
            onChange={(event) => edit(() => setAmountText(event.target.value))}
            inputMode="decimal"
            autoComplete="off"
            placeholder={formatReaisInput(Math.min(refundable, 1234))}
            className="figures"
          />
        </Field>
      ) : null}

      <Field label={t("payments.refund.reasonLabel")}>
        <Textarea value={reason} onChange={(event) => edit(() => setReason(event.target.value))} rows={2} maxLength={MAX_REASON} />
      </Field>

      {formError ? (
        <p role="alert" className="text-sm text-danger">
          {formError}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose} disabled={isPending}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={isPending}>
          {isPending ? t("payments.refund.submitting") : t("payments.refund.submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}
