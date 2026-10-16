import type { EntryStatus, PostingOut } from "../../api/generated/model";
import type { BadgeProps } from "../../components/ui/badge";
import type { MoneyValue } from "../../lib/money";

type Tone = NonNullable<BadgeProps["tone"]>;

/** Recipes the dictionary names (docs/FINANCIAL_MODEL.md section 4); any other code is shown as it is. */
export const RECIPES = ["R1", "R2", "R2a", "R2b", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11", "R12", "R13"] as const;

/** The filter offers the recipes the payment flow books today. */
export const FILTER_RECIPES = ["R1", "R2", "R2a", "R2b", "R3", "R4", "R8"] as const;

export const ENTRY_STATUSES: readonly EntryStatus[] = ["posted", "pending", "archived"];

export function entryTone(status: string): Tone {
  switch (status) {
    case "posted":
      return "success";
    case "pending":
      return "warning";
    default:
      return "neutral";
  }
}

export interface PostingTotals {
  currency: string;
  debits: bigint;
  credits: bigint;
}

/** Debit and credit totals per currency, summed as integers (never floats). */
export function totalsByCurrency(postings: readonly PostingOut[]): PostingTotals[] {
  const byCurrency = new Map<string, PostingTotals>();
  for (const posting of postings) {
    const currency = posting.amount.currency;
    const totals = byCurrency.get(currency) ?? { currency, debits: 0n, credits: 0n };
    const amount = BigInt(posting.amount.amount_minor);
    if (posting.direction === "debit") totals.debits += amount;
    else totals.credits += amount;
    byCurrency.set(currency, totals);
  }
  return [...byCurrency.values()];
}

export function isBalanced(totals: readonly PostingTotals[]): boolean {
  return totals.every((entry) => entry.debits === entry.credits);
}

export function minor(amount: bigint | number, currency: string): MoneyValue {
  return { amount_minor: String(amount), currency };
}

const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The API's typed id for a plain UUID (TypeID: 26 Crockford base32 characters), e.g. a payment UUID -> `pi_...`. */
export function typedId(prefix: string, uuid: string): string | null {
  if (!UUID_PATTERN.test(uuid)) return null;
  const number = BigInt(`0x${uuid.replaceAll("-", "")}`);
  let suffix = "";
  for (let shift = 25; shift >= 0; shift -= 1) {
    suffix += ALPHABET.charAt(Number((number >> BigInt(5 * shift)) & 31n));
  }
  return `${prefix}_${suffix}`;
}

/**
 * The payment (intent) an entry belongs to. The payment flow posts with the intent id as the correlation id, so
 * that is the reliable link; `metadata.payment_id` is only a fallback (it does not always name a payment).
 */
export function paymentIdOf(entry: { correlation_id: string | null; metadata: Record<string, unknown> }): string | null {
  const candidates = [entry.correlation_id, entry.metadata.payment_id];
  for (const raw of candidates) {
    if (typeof raw !== "string") continue;
    const id = raw.startsWith("pi_") ? raw : typedId("pi", raw);
    if (id) return id;
  }
  return null;
}
