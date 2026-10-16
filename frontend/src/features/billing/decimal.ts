import type { Locale } from "../../lib/locale";

/*
 * Usage values are exact decimal strings ("1234567.500000000"). They are formatted by string math only:
 * a float would round them (and a unique_count or a sum of fractional units must show exactly what the API said).
 */
const SEPARATORS: Record<Locale, { group: string; decimal: string }> = {
  "pt-BR": { group: ".", decimal: "," },
  "en-US": { group: ",", decimal: "." },
};

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/** "1234567.500000000" -> "1.234.567,5" (pt-BR) / "1,234,567.5" (en-US). Anything that is not a plain decimal is returned as is. */
export function formatDecimalString(value: string, locale: Locale): string {
  const match = DECIMAL.exec(value.trim());
  if (!match) return value;
  const { group, decimal } = SEPARATORS[locale];
  const sign = match[1] ?? "";
  const integer = (match[2] ?? "0").replace(/^0+(?=\d)/, "");
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  const negative = sign === "-" && (/[1-9]/.test(integer) || fraction !== "");
  return `${negative ? "-" : ""}${grouped}${fraction ? decimal + fraction : ""}`;
}
