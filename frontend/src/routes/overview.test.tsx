import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { apiKey, LIST, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const health = {
  "GET /health/live": () => json(200, { status: "live" }),
  "GET /health/ready": () => json(200, { status: "ready" }),
};

describe("Overview screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("summarises the active project", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, ME), ...health, ...overviewData("p-1") });
    const { container } = renderApp("/");
    expect(await screen.findByText("Balanced")).toBeInTheDocument();
    expect(screen.getByText("BRL")).toBeInTheDocument();
    expect(screen.getAllByText("1")).toHaveLength(2); // one active key, one active provider account
    expect(screen.getByText("Offline")).toBeInTheDocument(); // jsdom has no EventSource
    await expectNoAxeViolations(container);
  });

  it("flags an unbalanced ledger and caps counts that have more pages", async () => {
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      ...overviewData("p-1"),
      "GET /v1/projects/p-1/ledger/trial-balance": () =>
        json(200, {
          as_of: null,
          balanced: false,
          lines: [],
          totals: [{ currency: "BRL", balanced: false, debits: 2, credits: 1 }],
        }),
      [`GET /v1/projects/p-1/api-keys${LIST}`]: () => json(200, page([apiKey()], true)),
    });
    renderApp("/");
    expect(await screen.findByText("Not balanced")).toBeInTheDocument();
    expect(await screen.findByText("1+")).toBeInTheDocument();
  });

  it("shows an error per card instead of failing the page", async () => {
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      ...overviewData("p-1"),
      "GET /v1/projects/p-1/ledger/trial-balance": () => problem(500, "Ledger offline"),
    });
    renderApp("/");
    expect(await screen.findByText("Ledger offline")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("shows an empty state with Create project when there is no project", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, { ...ME, memberships: [] }), ...health });
    renderApp("/");
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create project" })).toBeInTheDocument();
  });

  it("does not query what the role cannot read", async () => {
    window.localStorage.setItem("fh.project", "p-2");
    const { calls } = mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      "GET /v1/projects/p-2/ledger/trial-balance": () => json(200, { as_of: null, balanced: true, lines: [], totals: [] }),
      [`GET /v1/projects/p-2/provider-accounts${LIST}`]: () => json(200, page([])),
    });
    renderApp("/");
    expect(await screen.findByText("No entries yet.")).toBeInTheDocument();
    expect(calls.some((call) => call.key.includes("api-keys"))).toBe(false);
  });
});
