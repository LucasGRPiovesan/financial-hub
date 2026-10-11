import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, clearCsrfToken, getCsrfToken, isApiError, setCsrfToken, setUnauthorizedHandler } from "./api";

function respond(status: number, body?: unknown, headers: Record<string, string> = {}) {
  const init: ResponseInit = { status, headers };
  if (body === undefined) return new Response(null, init);
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "Content-Type": status >= 400 ? "application/problem+json" : "application/json", ...headers },
  });
}

function lastCall(fetchMock: ReturnType<typeof vi.fn>) {
  const [path, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit & { headers: Record<string, string> }];
  return { path, init };
}

describe("apiFetch", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    clearCsrfToken();
    setUnauthorizedHandler(null);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sends cookies and parses JSON", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(respond(200, { ok: 1 })));
    await expect(apiFetch("/v1/things")).resolves.toEqual({ ok: 1 });
    const { path, init } = lastCall(fetchMock);
    expect(path).toBe("/v1/things");
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("GET");
  });

  it("adds X-CSRF-Token on unsafe methods only", async () => {
    setCsrfToken("tok-1");
    fetchMock.mockImplementation(() => Promise.resolve(respond(200, {})));

    await apiFetch("/v1/x");
    expect(lastCall(fetchMock).init.headers["X-CSRF-Token"]).toBeUndefined();

    for (const method of ["POST", "PUT", "PATCH", "DELETE"] as const) {
      await apiFetch("/v1/x", { method, body: { a: 1 } });
      expect(lastCall(fetchMock).init.headers["X-CSRF-Token"], method).toBe("tok-1");
    }
  });

  it("serialises the body as JSON", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(respond(200, {})));
    await apiFetch("/v1/x", { method: "POST", body: { email: "a@b.c" } });
    const { init } = lastCall(fetchMock);
    expect(init.body).toBe('{"email":"a@b.c"}');
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("keeps the CSRF token in memory only", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    setCsrfToken("secret");
    expect(getCsrfToken()).toBe("secret");
    expect(setItem).not.toHaveBeenCalled();
    expect(JSON.stringify(window.localStorage)).not.toContain("secret");
    setItem.mockRestore();
  });

  it("returns undefined for 204", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(respond(204)));
    await expect(apiFetch("/v1/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });

  it("maps problem+json to ApiError", async () => {
    fetchMock.mockResolvedValue(
      respond(422, { type: "https://x/errors/validation", title: "Validation failed", detail: "email is required" }),
    );
    const error = await apiFetch("/v1/x").catch((e: unknown) => e);
    expect(isApiError(error)).toBe(true);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.type).toBe("https://x/errors/validation");
    expect(apiError.title).toBe("Validation failed");
    expect(apiError.detail).toBe("email is required");
    expect(apiError.isNetworkError).toBe(false);
  });

  it("falls back to the status text for non-JSON error bodies", async () => {
    fetchMock.mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    const error = (await apiFetch("/v1/x").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.title).toBe("Bad Gateway");
    expect(error.type).toBe("about:blank");
  });

  it("reads Retry-After seconds on 429", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(respond(429, { title: "Too many attempts" }, { "Retry-After": "42" })));
    const error = (await apiFetch("/v1/x").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(42);
  });

  it("reads an HTTP-date Retry-After", async () => {
    vi.useFakeTimers({ now: new Date("2026-10-10T12:00:00Z") });
    try {
      fetchMock.mockResolvedValue(
        respond(429, { title: "Slow down" }, { "Retry-After": "Sat, 10 Oct 2026 12:01:30 GMT" }),
      );
      const error = (await apiFetch("/v1/x").catch((e: unknown) => e)) as ApiError;
      expect(error.retryAfterSeconds).toBe(90);
    } finally {
      vi.useRealTimers();
    }
  });

  it("calls the unauthorized handler on 401 unless opted out", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockImplementation(() => Promise.resolve(respond(401, { title: "Unauthorized" })));

    await expect(apiFetch("/v1/things")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);

    await expect(apiFetch("/v1/auth/me", { redirectOn401: false })).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("turns a failed fetch into a network ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = (await apiFetch("/v1/x").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.isNetworkError).toBe(true);
  });

  it("lets aborts through untouched", async () => {
    fetchMock.mockRejectedValue(new DOMException("aborted", "AbortError"));
    await expect(apiFetch("/v1/x")).rejects.toMatchObject({ name: "AbortError" });
  });
});
