import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { formatMoney, isNegativeMoney, MoneyParseError, minorUnitExponent, parseMoneyInput } from "./money";

/** Intl separates the symbol with NBSP / narrow NBSP; normalise so expectations stay readable. */
const plain = (text: string) => text.replace(/\s/g, " ");
const brl = (amount_minor: string) => plain(formatMoney({ amount_minor, currency: "BRL" }, { locale: "pt-BR" }));
const usd = (amount_minor: string) => plain(formatMoney({ amount_minor, currency: "USD" }, { locale: "en-US" }));

describe("formatMoney", () => {
  it("formats pt-BR and en-US", () => {
    expect(brl("123456")).toBe("R$ 1.234,56");
    expect(usd("123456")).toBe("$1,234.56");
  });

  it("pads the fraction and handles sub-unit amounts", () => {
    expect(usd("5")).toBe("$0.05");
    expect(usd("0")).toBe("$0.00");
    expect(usd("100")).toBe("$1.00");
    expect(brl("7")).toBe("R$ 0,07");
  });

  it("puts the sign in front", () => {
    expect(usd("-5")).toBe("-$0.05");
    expect(usd("-123456")).toBe("-$1,234.56");
  });

  it("is exact beyond Number.MAX_SAFE_INTEGER (no float path)", () => {
    expect(usd("9007199254740993")).toBe("$90,071,992,547,409.93");
    expect(usd("-9007199254740993")).toBe("-$90,071,992,547,409.93");
    expect(usd("123456789012345678901234567890")).toBe("$1,234,567,890,123,456,789,012,345,678.90");
    expect(usd("99999999999999999")).toBe("$999,999,999,999,999.99");
  });

  it("honours each currency's minor-unit exponent", () => {
    expect(plain(formatMoney({ amount_minor: "12345", currency: "JPY" }, { locale: "en-US" }))).toBe("¥12,345");
    expect(plain(formatMoney({ amount_minor: "12345", currency: "KWD" }, { locale: "en-US" }))).toBe("KWD 12.345");
    expect(minorUnitExponent("JPY")).toBe(0);
    expect(minorUnitExponent("KWD")).toBe(3);
    expect(minorUnitExponent("BRL")).toBe(2);
  });

  it("rejects values that are not integer strings", () => {
    for (const bad of ["1.5", "1e3", "", " 1", "0x10", "NaN", "--1"]) {
      expect(() => formatMoney({ amount_minor: bad, currency: "USD" }, { locale: "en-US" })).toThrow(RangeError);
    }
  });

  it("rejects malformed currency codes", () => {
    expect(() => formatMoney({ amount_minor: "1", currency: "usd" })).toThrow(RangeError);
  });
});

describe("isNegativeMoney", () => {
  it("is true only below zero", () => {
    expect(isNegativeMoney({ amount_minor: "-1", currency: "USD" })).toBe(true);
    expect(isNegativeMoney({ amount_minor: "0", currency: "USD" })).toBe(false);
    expect(isNegativeMoney({ amount_minor: "-0", currency: "USD" })).toBe(false);
    expect(isNegativeMoney({ amount_minor: "9007199254740993", currency: "USD" })).toBe(false);
  });
});

describe("parseMoneyInput", () => {
  it("parses pt-BR and en-US notation", () => {
    expect(parseMoneyInput("1.234,56", "BRL")).toBe(123456n);
    expect(parseMoneyInput("1,234.56", "USD")).toBe(123456n);
    expect(parseMoneyInput("1234,56", "BRL")).toBe(123456n);
    expect(parseMoneyInput("1234.56", "USD")).toBe(123456n);
  });

  it("treats a lone separator followed by three digits as grouping", () => {
    expect(parseMoneyInput("1.234", "BRL")).toBe(123400n);
    expect(parseMoneyInput("1,234", "USD")).toBe(123400n);
    expect(parseMoneyInput("1.234.567", "BRL")).toBe(123456700n);
  });

  it("treats a lone separator followed by 1-2 digits as the decimal point", () => {
    expect(parseMoneyInput("5,5", "BRL")).toBe(550n);
    expect(parseMoneyInput("0.05", "USD")).toBe(5n);
    expect(parseMoneyInput(",5", "BRL")).toBe(50n);
  });

  it("parses integers, symbols, signs and parentheses", () => {
    expect(parseMoneyInput("10", "BRL")).toBe(1000n);
    expect(parseMoneyInput("R$ 10", "BRL")).toBe(1000n);
    expect(parseMoneyInput("R$ 1.234,56", "BRL")).toBe(123456n);
    expect(parseMoneyInput("$1,234.56", "USD")).toBe(123456n);
    expect(parseMoneyInput("-5,5", "BRL")).toBe(-550n);
    expect(parseMoneyInput("-R$ 5,50", "BRL")).toBe(-550n);
    expect(parseMoneyInput("−5", "USD")).toBe(-500n);
    expect(parseMoneyInput("+5", "USD")).toBe(500n);
    expect(parseMoneyInput("(12,00)", "BRL")).toBe(-1200n);
  });

  it("is exact for amounts beyond 2^53", () => {
    expect(parseMoneyInput("90.071.992.547.409,93", "BRL")).toBe(9007199254740993n);
    expect(parseMoneyInput("123456789012345678901234567890", "USD")).toBe(12345678901234567890123456789000n);
  });

  it("respects the currency exponent", () => {
    expect(parseMoneyInput("12345", "JPY")).toBe(12345n);
    expect(parseMoneyInput("12.345", "KWD")).toBe(12345n);
    expect(parseMoneyInput("1.234", "JPY")).toBe(1234n);
    expect(parseMoneyInput("1,5", "KWD")).toBe(1500n);
  });

  it("round-trips with formatMoney", () => {
    for (const minor of ["0", "1", "99", "100", "123456", "9007199254740993", "-250"]) {
      const text = formatMoney({ amount_minor: minor, currency: "BRL" }, { locale: "pt-BR" });
      expect(parseMoneyInput(text, "BRL").toString()).toBe(minor);
    }
  });

  it("rejects instead of rounding", () => {
    expect(() => parseMoneyInput("1,234", "JPY")).not.toThrow(); // grouping, not a fraction
    expect(() => parseMoneyInput("1,23", "JPY")).toThrow(MoneyParseError);
    expect(() => parseMoneyInput("1.2345,6", "BRL")).toThrow(MoneyParseError);
    expect(() => parseMoneyInput("1,2345", "BRL")).toThrow(MoneyParseError);
    expect(() => parseMoneyInput("10,", "BRL")).toThrow(MoneyParseError);
  });

  it("rejects garbage", () => {
    for (const bad of ["", "abc", "1.2.3,4.5", "1,23,456", "12e3", "1x2", "--5", ".", ","]) {
      expect(() => parseMoneyInput(bad, "BRL"), bad).toThrow(MoneyParseError);
    }
  });
});

describe("money module has no float path", () => {
  const forbidden: ReadonlyArray<[string, RegExp]> = [
    ["Number(", /\bNumber\s*\(/],
    ["Number.parse*", /\bNumber\s*\.\s*parse/],
    ["parseFloat", /\bparseFloat\b/],
    ["parseInt", /\bparseInt\b/],
    ["Math.round/floor/ceil/trunc", /\bMath\s*\.\s*(round|floor|ceil|trunc)\b/],
    ["toFixed/toPrecision", /\.(toFixed|toPrecision)\s*\(/],
    ["unary plus coercion", /(?:^|[=(,:?&|!]\s*)\+\s*[A-Za-z_$(]/m],
  ];

  for (const file of ["lib/money.ts", "components/money.tsx"]) {
    const source = readFileSync(resolve(__dirname, "..", file), "utf8")
      // Comments may discuss the banned functions.
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");
    for (const [name, pattern] of forbidden) {
      it(`${file} does not use ${name}`, () => {
        expect(source).not.toMatch(pattern);
      });
    }
  }
});
