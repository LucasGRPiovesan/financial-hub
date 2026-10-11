/*
 * Money (ADR-0002): an amount is an integer count of minor units plus an ISO 4217 code.
 *
 * The API sends `amount_minor` as a STRING (it can exceed 2^53). Everything here goes through
 * BigInt and string manipulation; Intl is used only for digit grouping and the currency symbol,
 * and only ever receives BigInt or already-split digit strings. Converting an amount to a
 * floating-point value anywhere in this module is a bug: eslint (no-restricted-syntax in
 * eslint.config.js) and money.test.ts both forbid the coercion functions in this file.
 */

export interface MoneyValue {
  /** Integer minor units as a decimal string, e.g. "123456" = 1,234.56 BRL. May be negative. */
  amount_minor: string;
  /** ISO 4217 alphabetic code, e.g. "BRL". */
  currency: string;
}

export class MoneyParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MoneyParseError";
  }
}

const ZERO_DECIMAL = new Set([
  "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
]);
const THREE_DECIMAL = new Set(["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"]);
const FOUR_DECIMAL = new Set(["CLF", "UYW"]);

const INTEGER_STRING = /^-?\d+$/;
const CURRENCY_CODE = /^[A-Z]{3}$/;

/** Number of fraction digits of the currency's minor unit (ISO 4217 exponent). */
export function minorUnitExponent(currency: string): number {
  if (!CURRENCY_CODE.test(currency)) throw new RangeError(`Invalid currency code: ${currency}`);
  if (ZERO_DECIMAL.has(currency)) return 0;
  if (THREE_DECIMAL.has(currency)) return 3;
  if (FOUR_DECIMAL.has(currency)) return 4;
  return 2;
}

/** Parses the API's string amount into a bigint of minor units. */
export function parseMinorUnits(amountMinor: string): bigint {
  if (!INTEGER_STRING.test(amountMinor)) {
    throw new RangeError(`amount_minor must be an integer string, got: ${JSON.stringify(amountMinor)}`);
  }
  return BigInt(amountMinor);
}

export interface FormatMoneyOptions {
  /** BCP 47 locale; defaults to the operator's browser locale. */
  locale?: string;
}

function defaultLocale(): string {
  return typeof navigator !== "undefined" && navigator.language ? navigator.language : "en-US";
}

const formatterCache = new Map<string, Intl.NumberFormat>();

function wholeFormatter(locale: string, currency: string): Intl.NumberFormat {
  const key = `${locale}|${currency}`;
  let formatter = formatterCache.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
    formatterCache.set(key, formatter);
  }
  return formatter;
}

function decimalSeparator(locale: string): string {
  const probe = new Intl.NumberFormat(locale, { minimumFractionDigits: 1 }).formatToParts(11n);
  return probe.find((part) => part.type === "decimal")?.value ?? ".";
}

/** Splits minor units into whole units and a zero-padded fraction without any arithmetic on floats. */
export function splitMinorUnits(minor: bigint, exponent: number): { negative: boolean; whole: bigint; fraction: string } {
  const negative = minor < 0n;
  const magnitude = negative ? -minor : minor;
  if (exponent === 0) return { negative, whole: magnitude, fraction: "" };
  const scale = 10n ** BigInt(exponent);
  return {
    negative,
    whole: magnitude / scale,
    fraction: (magnitude % scale).toString().padStart(exponent, "0"),
  };
}

/** Formats {amount_minor, currency} for display, e.g. "R$ 1.234,56" (pt-BR) or "-$0.05" (en-US). */
export function formatMoney(value: MoneyValue, options: FormatMoneyOptions = {}): string {
  const locale = options.locale ?? defaultLocale();
  const exponent = minorUnitExponent(value.currency);
  const { negative, whole, fraction } = splitMinorUnits(parseMinorUnits(value.amount_minor), exponent);

  // Intl formats only the whole part (a BigInt, so exact) and supplies grouping and the symbol;
  // the fraction digits are spliced in after the last integer part.
  const parts = wholeFormatter(locale, value.currency).formatToParts(whole);
  let lastInteger = -1;
  parts.forEach((part, index) => {
    if (part.type === "integer") lastInteger = index;
  });
  const pieces = parts.map((part) => part.value);
  if (fraction !== "" && lastInteger >= 0) {
    pieces[lastInteger] = `${pieces[lastInteger] ?? ""}${decimalSeparator(locale)}${fraction}`;
  }
  return `${negative ? "-" : ""}${pieces.join("")}`;
}

/** True when the amount is below zero (for negative styling). Zero, including "-0", is not negative. */
export function isNegativeMoney(value: MoneyValue): boolean {
  return parseMinorUnits(value.amount_minor) < 0n;
}

function stripCurrencyAffixes(text: string): string {
  return text.replace(/^[\p{Sc}A-Za-z]+/u, "").replace(/[\p{Sc}A-Za-z]+$/u, "");
}

/**
 * Parses operator-typed text such as "1.234,56", "1,234.56", "R$ 10", "-5,5" or "(12,00)" into minor units.
 *
 * The decimal separator is inferred: when both "." and "," appear the last one is the decimal point;
 * a single separator that appears once is a decimal point unless it is followed by exactly three digits
 * (then it groups thousands, "1.234" = 1234) or the currency has three decimals. More fraction digits than
 * the currency's exponent is an error, never a silent rounding.
 */
export function parseMoneyInput(input: string, currency: string): bigint {
  const exponent = minorUnitExponent(currency);
  // \s also matches NBSP and narrow NBSP (U+00A0, U+202F), which Intl uses as separators.
  let text = input.replace(/\s/g, "");
  let negative = false;

  if (text.startsWith("(") && text.endsWith(")")) {
    negative = true;
    text = text.slice(1, -1);
  }
  text = stripCurrencyAffixes(text);
  const sign = /^[-\u2212+]/.exec(text)?.[0];
  if (sign !== undefined) {
    if (sign !== "+") negative = !negative;
    text = stripCurrencyAffixes(text.slice(1));
  }
  if (!/^[\d.,]+$/.test(text)) throw new MoneyParseError(`Not a valid amount: ${JSON.stringify(input)}`);

  const dots = text.split(".").length - 1;
  const commas = text.split(",").length - 1;
  let decimal: "." | "," | null = null;
  if (dots > 0 && commas > 0) {
    decimal = text.lastIndexOf(".") > text.lastIndexOf(",") ? "." : ",";
  } else if (dots > 0 || commas > 0) {
    const separator = dots > 0 ? "." : ",";
    const occurrences = dots > 0 ? dots : commas;
    const digitsAfter = text.length - text.lastIndexOf(separator) - 1;
    const groups = occurrences > 1 || (digitsAfter === 3 && exponent !== 3);
    decimal = groups ? null : separator;
  }

  let integerText = text;
  let fractionText = "";
  if (decimal !== null) {
    const at = text.lastIndexOf(decimal);
    integerText = text.slice(0, at);
    fractionText = text.slice(at + 1);
    if (fractionText === "" || fractionText.length > exponent) {
      throw new MoneyParseError(`${currency} allows at most ${exponent} fraction digits: ${JSON.stringify(input)}`);
    }
  }
  if (/[.,]/.test(integerText)) {
    const group = integerText.includes(".") ? "." : ",";
    const grouped = new RegExp(`^\\d{1,3}(?:\\${group}\\d{3})+$`);
    if (integerText.includes(group === "." ? "," : ".") || !grouped.test(integerText)) {
      throw new MoneyParseError(`Misplaced thousands separator: ${JSON.stringify(input)}`);
    }
    integerText = integerText.split(group).join("");
  }
  if (integerText === "" && fractionText === "") throw new MoneyParseError("Enter an amount");

  const whole = BigInt(integerText === "" ? "0" : integerText);
  const fraction = BigInt(fractionText.padEnd(exponent, "0") === "" ? "0" : fractionText.padEnd(exponent, "0"));
  const minor = whole * 10n ** BigInt(exponent) + fraction;
  return negative ? -minor : minor;
}
