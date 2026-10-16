import type { PaymentOut } from "../../api/generated/model";

export const KPI_WINDOW_DAYS = 30;
export const KPI_CURRENCY = "BRL";

export interface PaymentsKpi {
  count: number;
  /** Sum of captured minor units in BRL, as a decimal string (BigInt: never a float). */
  capturedMinor: string;
}

/** Payments created in the last 30 days, and what they captured in BRL (integer minor units summed with BigInt). */
export function paymentsKpi(payments: readonly PaymentOut[], now: Date = new Date()): PaymentsKpi {
  const since = now.getTime() - KPI_WINDOW_DAYS * 24 * 60 * 60 * 1000;
  let count = 0;
  let total = 0n;
  for (const payment of payments) {
    if (Date.parse(payment.created_at) < since) continue;
    count += 1;
    if (payment.currency === KPI_CURRENCY && Number.isSafeInteger(payment.captured_amount)) total += BigInt(payment.captured_amount);
  }
  return { count, capturedMinor: total.toString() };
}
