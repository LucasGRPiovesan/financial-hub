import { formatMoney, isNegativeMoney, type MoneyValue } from "../lib/money";
import { cn } from "../lib/cn";

interface MoneyProps {
  value: MoneyValue;
  locale?: string;
  className?: string;
}

/** An amount in a ledger column: tabular numerals, negatives in the danger colour. */
export function Money({ value, locale, className }: MoneyProps) {
  let text: string;
  let negative: boolean;
  try {
    text = formatMoney(value, { locale });
    negative = isNegativeMoney(value);
  } catch {
    return (
      <span className={cn("figures text-ink-faint", className)} title="This amount could not be read">
        &mdash;
      </span>
    );
  }
  return (
    <span
      className={cn("figures whitespace-nowrap", negative && "text-danger", className)}
      data-negative={negative || undefined}
      title={`${value.amount_minor} ${value.currency} minor units`}
    >
      {text}
    </span>
  );
}
