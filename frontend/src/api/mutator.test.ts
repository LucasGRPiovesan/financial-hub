import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, clearCsrfToken, isProblem, problemMessage, setCsrfToken, setUnauthorizedHandler } from "../lib/api";
import { apiMutator } from "./mutator";

const fetchMock = vi.fn();

function problem(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/problem+json" } });
}
function lastInit() {
  return fetchMock.mock.calls.at(-1)?.[1] as RequestInit & { headers: Record<string, string> };
}

describe("apiMutator", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    clearCsrfToken();
    setUnauthorizedHandler(null);
    setCsrfToken("csrf-1");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("adds the CSRF header and credentials on POST, not on GET", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    await apiMutator("/v1/x", { method: "post", body: JSON.stringify({ a: 1 }) });
    expect(lastInit().headers["X-CSRF-Token"]).toBe("csrf-1");
    expect(lastInit().credentials).toBe("include");
    expect(lastInit().body).toBe('{"a":1}');
    await apiMutator("/v1/x", { method: "get" });
    expect(lastInit().headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("parses problem+json into an ApiProblem", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        problem(422, {
          type: "https://financial-hub.dev/problems/validation",
          title: "Validation failed",
          detail: "Bad input",
          instance: "/v1/x",
          errors: [{ pointer: "/name", detail: "required" }],
        }),
      ),
    );
    const error: unknown = await apiMutator("/v1/x", { method: "POST", body: "{}" }).catch((e: unknown) => e);
    expect(isProblem(error)).toBe(true);
    expect(error).toBeInstanceOf(ApiError);
    const typed = error as ApiError;
    expect(typed.status).toBe(422);
    expect(typed.instance).toBe("/v1/x");
    expect(typed.errors).toEqual([{ pointer: "/name", detail: "required" }]);
    expect(problemMessage(error)).toBe("Bad input");
    expect(problemMessage(new Error("x"), "fallback")).toBe("fallback");
  });

  it("calls the unauthorized handler on 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockImplementation(() => Promise.resolve(problem(401, { title: "Unauthorized" })));
    await expect(apiMutator("/v1/x")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
