import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { apiKey, AUDIT, customer, CUSTOMERS, LIST, member, overviewData, page, problem } from "../test/console-mocks";
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
    // one active key, provider account, member and customer
    expect(screen.getAllByText("1")).toHaveLength(4);
    expect(await screen.findByText("api_key.created")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Recent activity" })).toBeInTheDocument();
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

  it("shows debits and credits per currency as money", async () => {
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      ...overviewData("p-1"),
      "GET /v1/projects/p-1/ledger/trial-balance": () =>
        json(200, {
          as_of: null,
          balanced: true,
          lines: [],
          totals: [{ currency: "BRL", balanced: true, debits: 123456, credits: 123456 }],
        }),
    });
    renderApp("/");
    const table = await screen.findByRole("table", { name: "Trial balance by currency" });
    const row = within(table).getByRole("row", { name: /BRL/ });
    expect(within(row).getAllByText(/1.234,56|1,234.56/)).toHaveLength(2);
    expect(within(row).getByText("Balanced")).toBeInTheDocument();
  });

  it("caps the customer and member counts and shows an empty activity log", async () => {
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      ...overviewData("p-1"),
      [`GET /v1/projects/p-1/customers${CUSTOMERS}`]: () => json(200, { ...page([customer()], true), next_cursor: "c2" }),
      "GET /v1/projects/p-1/members": () => json(200, { data: [member(), member({ operator_id: "op-2", email: "b@example.test" })] }),
      [`GET /v1/projects/p-1/audit-records${AUDIT}`]: () => json(200, page([])),
    });
    renderApp("/");
    expect(await screen.findByText("1+")).toBeInTheDocument();
    expect(await screen.findByText("2")).toBeInTheDocument();
    expect(await screen.findByText("No activity recorded yet.")).toBeInTheDocument();
  });

  it("explains a failed activity log without breaking the other cards", async () => {
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      ...health,
      ...overviewData("p-1"),
      [`GET /v1/projects/p-1/audit-records${AUDIT}`]: () => problem(500, "Audit log unavailable"),
    });
    renderApp("/");
    expect(await screen.findByText("Audit log unavailable")).toBeInTheDocument();
    expect(await screen.findByText("Balanced")).toBeInTheDocument();
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
      "GET /v1/projects/p-2/members": () => json(200, { data: [member({ role: "viewer" })] }),
      [`GET /v1/projects/p-2/customers${CUSTOMERS}`]: () => json(200, page([])),
    });
    renderApp("/");
    expect(await screen.findByText("No entries yet.")).toBeInTheDocument();
    expect(calls.some((call) => call.key.includes("api-keys"))).toBe(false);
    expect(calls.some((call) => call.key.includes("audit-records"))).toBe(false);
    expect(screen.getAllByText("Not available for your role.").length).toBeGreaterThanOrEqual(2);
  });
});
