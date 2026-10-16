import { useI18n } from "../i18n";
import { formatMoney, isNegativeMoney, type MoneyValue } from "../lib/money";
import { cn } from "../lib/cn";

interface MoneyProps {
  value: MoneyValue;
  locale?: string;
  className?: string;
}

/** An amount in a ledger column: tabular numerals, negatives in the danger colour. */
export function Money({ value, locale, className }: MoneyProps) {
  const { locale: current, t } = useI18n();
  let text: string;
  let negative: boolean;
  try {
    text = formatMoney(value, { locale: locale ?? current });
    negative = isNegativeMoney(value);
  } catch {
    return (
      <span className={cn("figures text-ink-faint", className)} title={t("money.unreadable")}>
        &mdash;
      </span>
    );
  }
  return (
    <span
      className={cn("figures whitespace-nowrap", negative && "text-danger", className)}
      data-negative={negative || undefined}
      title={t("money.minorUnits", { amount: value.amount_minor, currency: value.currency })}
    >
      {text}
    </span>
  );
}
