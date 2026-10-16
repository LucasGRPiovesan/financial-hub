/*
 * Operator-typed refund amounts in reais -> integer centavos, by string and BigInt math only (ADR-0002).
 *
 * Accepted: "12", "12,34", "1.234,56", "12.34", "1,234.56", with an optional "R$". Rejected: more than two
 * decimals ("12,345"), misplaced separators, signs, zero. A lone "1.234" is thousands grouping (pt-BR), never
 * 1.234 reais; "12,345" is rejected rather than guessed.
 */

export type RefundAmountError = "empty" | "invalid" | "decimals" | "zero";

export type RefundAmountResult = { ok: true; centavos: bigint } | { ok: false; error: RefundAmountError };

const PT_BR = /^(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?$/;
const DOT_DECIMAL = /^(\d+)\.(\d{1,2})$/;
const EN_GROUPED = /^(\d{1,3}(?:,\d{3})+)\.(\d{1,2})$/;

export function parseRefundAmount(input: string): RefundAmountResult {
  const text = input.replace(/\s/g, "").replace(/^R\$/i, "");
  if (text === "") return { ok: false, error: "empty" };

  let whole: string;
  let fraction: string;
  const pt = PT_BR.exec(text);
  const dot = DOT_DECIMAL.exec(text);
  const en = EN_GROUPED.exec(text);
  if (pt) {
    whole = (pt[1] ?? "").replaceAll(".", "");
    fraction = pt[2] ?? "";
  } else if (dot) {
    whole = dot[1] ?? "";
    fraction = dot[2] ?? "";
  } else if (en) {
    whole = (en[1] ?? "").replaceAll(",", "");
    fraction = en[2] ?? "";
  } else {
    return { ok: false, error: /[.,]\d{3,}$/.test(text) ? "decimals" : "invalid" };
  }
  const centavos = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0") || "0");
  if (centavos <= 0n) return { ok: false, error: "zero" };
  return { ok: true, centavos };
}

/** Centavos as the plain reais text the input shows ("1234" -> "12,34"), for "refund everything refundable". */
export function formatReaisInput(centavos: number): string {
  const digits = String(centavos).padStart(3, "0");
  return `${digits.slice(0, -2)},${digits.slice(-2)}`;
}
