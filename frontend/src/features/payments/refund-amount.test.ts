import { describe, expect, it } from "vitest";

import { formatReaisInput, parseRefundAmount } from "./refund-amount";

describe("parseRefundAmount", () => {
  it.each([
    ["12,34", 1234n],
    ["1.234,56", 123456n],
    ["12", 1200n],
    ["0,05", 5n],
    ["12,3", 1230n],
    ["R$ 10,00", 1000n],
    ["12.34", 1234n],
    ["1,234.56", 123456n],
    ["1.234", 123400n],
  ])("converts %s to %s centavos", (input, centavos) => {
    expect(parseRefundAmount(input)).toEqual({ ok: true, centavos });
  });

  it("converts amounts beyond the float-safe range exactly", () => {
    expect(parseRefundAmount("90.071.992.547.409,93")).toEqual({ ok: true, centavos: 9007199254740993n });
  });

  it("rejects more than two decimal places", () => {
    expect(parseRefundAmount("12,345")).toEqual({ ok: false, error: "decimals" });
    expect(parseRefundAmount("1.234,567")).toEqual({ ok: false, error: "decimals" });
  });

  it("rejects empty, zero, signed and malformed input", () => {
    expect(parseRefundAmount("  ")).toEqual({ ok: false, error: "empty" });
    expect(parseRefundAmount("0")).toEqual({ ok: false, error: "zero" });
    expect(parseRefundAmount("0,00")).toEqual({ ok: false, error: "zero" });
    expect(parseRefundAmount("-5,00")).toEqual({ ok: false, error: "invalid" });
    expect(parseRefundAmount("abc")).toEqual({ ok: false, error: "invalid" });
    expect(parseRefundAmount("1,2,3")).toEqual({ ok: false, error: "invalid" });
  });
});

describe("formatReaisInput", () => {
  it("renders centavos as plain reais text", () => {
    expect(formatReaisInput(1234)).toBe("12,34");
    expect(formatReaisInput(5)).toBe("0,05");
    expect(formatReaisInput(100)).toBe("1,00");
  });
});
