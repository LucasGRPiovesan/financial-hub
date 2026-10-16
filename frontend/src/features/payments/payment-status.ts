import type { PaymentOut } from "../../api/generated/model";
import type { BadgeProps } from "../../components/ui/badge";
import { camelKey } from "../../i18n";
import type { MoneyValue } from "../../lib/money";

type Tone = NonNullable<BadgeProps["tone"]>;

/**
 * The API derives one `status` from four orthogonal fields (capture, settlement, refund, dispute; ADR-0012).
 * The list shows that status as the primary badge; the detail also spells out the underlying fields.
 */
export const PAYMENT_STATUSES = [
  "requires_payment",
  "processing",
  "overdue",
  "failed",
  "canceled",
  "succeeded",
  "settled",
  "partially_refunded",
  "refunded",
  "disputed",
  "charged_back",
] as const;

const TONES: Record<string, Tone> = {
  requires_payment: "info",
  processing: "info",
  overdue: "warning",
  failed: "danger",
  canceled: "neutral",
  succeeded: "primary",
  settled: "success",
  partially_refunded: "warning",
  refunded: "neutral",
  disputed: "warning",
  charged_back: "danger",
};

export function statusTone(status: string): Tone {
  return TONES[status] ?? "neutral";
}

/** Dictionary key (under `payments.status.`) of a status or underlying field value. */
export function statusKey(status: string): string {
  return camelKey(status);
}

export const PAYMENT_METHODS = ["pix", "boleto", "card"] as const;

/** Method badge text; the brand names stay as they are in both languages. */
export function methodKey(method: string): string {
  return camelKey(method);
}

export interface StateDetail {
  /** Which orthogonal field this is: "capture" | "settlement" | "refund" | "dispute". */
  field: "capture" | "settlement" | "refund" | "dispute";
  /** The field's raw value ("captured", "partial", ...). */
  value: string;
}

/** The secondary states worth showing next to the primary badge: everything that is not the neutral default. */
export function secondaryStates(payment: Pick<PaymentOut, "capture_status" | "settlement_status" | "refund_status" | "dispute_status">): StateDetail[] {
  const details: StateDetail[] = [{ field: "capture", value: payment.capture_status }];
  if (payment.settlement_status !== "none") details.push({ field: "settlement", value: payment.settlement_status });
  if (payment.refund_status !== "none") details.push({ field: "refund", value: payment.refund_status });
  if (payment.dispute_status !== "none") details.push({ field: "dispute", value: payment.dispute_status });
  return details;
}

export type RefundTone = Tone;
export function refundTone(status: string): Tone {
  switch (status) {
    case "done":
      return "success";
    case "denied":
    case "cancelled":
      return "danger";
    default:
      return "info";
  }
}

/** An API amount (integer minor units, a JSON number) as the string-based MoneyValue the formatter takes. */
export function moneyOf(amount: number, currency: string): MoneyValue {
  return { amount_minor: String(amount), currency };
}
