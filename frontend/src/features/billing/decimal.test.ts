import { describe, expect, it } from "vitest";

import { formatDecimalString } from "./decimal";

describe("formatDecimalString", () => {
  it("groups thousands and trims trailing zeros without going through a float", () => {
    expect(formatDecimalString("1234567.500000000", "pt-BR")).toBe("1.234.567,5");
    expect(formatDecimalString("1234567.500000000", "en-US")).toBe("1,234,567.5");
  });

  it("keeps digits beyond float precision", () => {
    expect(formatDecimalString("12345678901234567890.123456789", "en-US")).toBe("12,345,678,901,234,567,890.123456789");
  });

  it("handles integers, zero and negatives", () => {
    expect(formatDecimalString("1000", "pt-BR")).toBe("1.000");
    expect(formatDecimalString("0.000000000", "en-US")).toBe("0");
    expect(formatDecimalString("-1234.25", "pt-BR")).toBe("-1.234,25");
  });
});
