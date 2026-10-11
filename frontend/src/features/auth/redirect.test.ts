import { describe, expect, it } from "vitest";

import { safeRedirectTarget } from "./redirect";

describe("safeRedirectTarget", () => {
  it("keeps same-origin app paths, with query and hash", () => {
    expect(safeRedirectTarget("/payments")).toBe("/payments");
    expect(safeRedirectTarget("/ledger?account=1#top")).toBe("/ledger?account=1#top");
    expect(safeRedirectTarget("/")).toBe("/");
  });

  it("rejects anything that could leave the origin", () => {
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)", "evil", ""]) {
      expect(safeRedirectTarget(bad), bad).toBeUndefined();
    }
  });

  it("rejects non-strings and the login page itself", () => {
    expect(safeRedirectTarget(undefined)).toBeUndefined();
    expect(safeRedirectTarget(42)).toBeUndefined();
    expect(safeRedirectTarget("/login")).toBeUndefined();
    expect(safeRedirectTarget("/login?redirect=/x")).toBeUndefined();
  });
});
