import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { createQueryClient, shouldRetry } from "./query-client";

const err = (status: number) => new ApiError({ status, title: "t" });

describe("query client defaults", () => {
  it("never retries 4xx", () => {
    for (const status of [400, 401, 403, 404, 422, 429]) expect(shouldRetry(0, err(status))).toBe(false);
  });
  it("retries 5xx and network errors a bounded number of times", () => {
    expect(shouldRetry(0, err(503))).toBe(true);
    expect(shouldRetry(0, err(0))).toBe(true);
    expect(shouldRetry(2, err(503))).toBe(false);
  });
  it("installs the retry policy", () => {
    expect(createQueryClient().getDefaultOptions().queries?.retry).toBe(shouldRetry);
  });
});
