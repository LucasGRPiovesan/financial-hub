import { describe, expect, it } from "vitest";

import { formatDateTime, formatRelative, parseIsoInstant } from "./datetime";

describe("formatDateTime", () => {
  it("renders an ISO UTC instant in the requested zone with a zone label", () => {
    const utc = formatDateTime("2026-10-10T19:30:00Z", { locale: "en-US", timeZone: "UTC" });
    expect(utc.text.replace(/\s/g, " ")).toBe("Oct 10, 2026, 7:30:00 PM");
    expect(utc.zone).toBe("UTC");

    const sp = formatDateTime("2026-10-10T19:30:00Z", { locale: "en-US", timeZone: "America/Sao_Paulo" });
    expect(sp.text.replace(/\s/g, " ")).toBe("Oct 10, 2026, 4:30:00 PM");
    expect(sp.zone).toBe("GMT-3");
  });

  it("follows the locale", () => {
    const { text } = formatDateTime("2026-10-10T19:30:00Z", { locale: "pt-BR", timeZone: "America/Sao_Paulo" });
    expect(text).toContain("10");
    expect(text).toContain("16:30:00");
  });

  it("rejects invalid timestamps", () => {
    expect(() => formatDateTime("yesterday")).toThrow(RangeError);
    expect(() => parseIsoInstant("")).toThrow(RangeError);
  });
});

describe("formatRelative", () => {
  const now = new Date("2026-10-10T12:00:00Z");

  it("describes the past and the future", () => {
    expect(formatRelative("2026-10-10T11:57:00Z", now, "en-US")).toBe("3 minutes ago");
    expect(formatRelative("2026-10-10T09:00:00Z", now, "en-US")).toBe("3 hours ago");
    expect(formatRelative("2026-10-12T12:00:00Z", now, "en-US")).toBe("in 2 days");
    expect(formatRelative("2026-10-09T12:00:00Z", now, "en-US")).toBe("yesterday");
  });

  it("collapses the last seconds into 'now'", () => {
    expect(formatRelative("2026-10-10T11:59:40Z", now, "en-US")).toBe("now");
    expect(formatRelative("2026-10-10T12:00:20Z", now, "en-US")).toBe("now");
  });
});
