import { configure, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { typedId } from "../features/ledger/ledger-model";
import { setLocale } from "../lib/locale";
import { expectNoAxeViolations } from "../test/axe";
import { overviewData, page } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const PAYMENT_UUID = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b";

const TRIAL = {
  as_of: null,
  balanced: true,
  totals: [{ currency: "BRL", balanced: true, debits: 358540, credits: 358540 }],
  lines: [
    { currency: "BRL", type: "asset", code: "customer_receivable", debits: 120000, credits: 20000 },
    { currency: "BRL", type: "asset", code: "provider_balance", debits: 238540, credits: 100000 },
    { currency: "BRL", type: "income", code: "platform_revenue", debits: 0, credits: 100000 },
    { currency: "BRL", type: "liability", code: "sales_clearing", debits: 0, credits: 238540 },
  ],
};

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: "je_1",
    status: "posted",
    recipe: "R2",
    idempotency_key: "capture:pay-1",
    effective_at: "2026-10-03T09:00:00Z",
    created_at: "2026-10-03T09:00:01Z",
    reverses_entry_id: null,
    reversed_by_entry_id: null,
    correlation_id: "6a1f0c64-0000-4000-8000-000000000001",
    causation_id: null,
    metadata: { payment_id: PAYMENT_UUID, note: "[redacted]" },
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    ...entry(overrides),
    postings: [
      { id: "po_1", account_id: "a-1", account_code: "provider_receivable:prov-1", direction: "debit", amount: { amount_minor: 12990, currency: "BRL" } },
      { id: "po_2", account_id: "a-2", account_code: "sales_clearing", direction: "credit", amount: { amount_minor: 12990, currency: "BRL" } },
    ],
  };
}

const account = (overrides: Record<string, unknown> = {}) => ({
  id: "a-1",
  code: "customer_receivable:cus-1",
  type: "asset",
  normal_balance: "debit",
  currency: "BRL",
  no_overdraft: false,
  customer_id: "cus-1",
  payee_id: null,
  provider_account_id: null,
  balance: {
    posted: { amount_minor: 100000, currency: "BRL" },
    pending: { amount_minor: 0, currency: "BRL" },
    available: { amount_minor: 100000, currency: "BRL" },
  },
  created_at: "2026-10-01T10:00:00Z",
  ...overrides,
});

const meAs = (role: string) => ({
  ...ME,
  memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]],
});

function base(role = "owner") {
  return {
    "GET /v1/auth/me": () => json(200, meAs(role)),
    ...overviewData("p-1"),
    "GET /v1/projects/p-1/ledger/trial-balance": () => json(200, TRIAL),
    "GET /v1/projects/p-1/ledger/accounts?limit=50": () => json(200, page([account(), account({ id: "a-2", code: "platform_revenue", type: "income", normal_balance: "credit", customer_id: null })])),
    "GET /v1/projects/p-1/ledger/entries?limit=25": () => json(200, page([entry(), entry({ id: "je_2", recipe: "R8" })])),
    "GET /v1/projects/p-1/ledger/entries/je_1": () => json(200, detail()),
    "GET /v1/projects/p-1/ledger/entries/je_2": () => json(200, detail({ id: "je_2", recipe: "R8" })),
  };
}

describe("Ledger", () => {
  // Lazy route chunks load on first render; a busy CI host needs more than the 1 s default.
  beforeAll(() => {
    configure({ asyncUtilTimeout: 10_000 });
  });
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("shows a balanced trial balance per currency, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/ledger");

    expect(await screen.findByRole("heading", { name: "Ledger" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Trial balance, BRL" });
    expect(within(table).getByText("Customer receivable")).toBeInTheDocument();
    expect(within(table).getByText("Platform revenue")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
    expect(within(table).getByTestId("trial-debits")).toHaveTextContent("3,585.40");
    expect(within(table).getByTestId("trial-credits")).toHaveTextContent("3,585.40");
    await expectNoAxeViolations(container);
  }, 60_000); // first test pays the cold import of the lazy route chunks

  it("flags a trial balance that does not balance", async () => {
    mockApi({
      ...base(),
      "GET /v1/projects/p-1/ledger/trial-balance": () =>
        json(200, { ...TRIAL, balanced: false, totals: [{ currency: "BRL", balanced: false, debits: 100, credits: 90 }] }),
    });
    renderApp("/ledger");
    expect(await screen.findByText("Out of balance")).toBeInTheDocument();
    expect(screen.getByText(/never happen/)).toBeInTheDocument();
  });

  it("opens the chart of accounts from the URL and filters it by text", async () => {
    mockApi(base());
    renderApp("/ledger?tab=accounts");

    const table = await screen.findByRole("table", { name: "Chart of accounts" });
    expect(within(table).getByText("Customer receivable")).toBeInTheDocument();
    expect(within(table).getByText("Platform revenue")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search accounts"), "revenue");
    expect(within(table).queryByText("Customer receivable")).not.toBeInTheDocument();
    expect(within(table).getByText("Platform revenue")).toBeInTheDocument();
  });

  it("lists journal entries with the recipe name in English", async () => {
    mockApi(base());
    renderApp("/ledger?tab=entries");

    const table = await screen.findByRole("table", { name: "Journal entries" });
    expect(await within(table).findByText("Payment captured")).toBeInTheDocument();
    expect(within(table).getByText("Refund")).toBeInTheDocument();
    expect(within(table).getAllByText("Posted").length).toBe(2);
    expect(await within(table).findAllByText(/129.90/)).toHaveLength(2);
  });

  it("names the recipes in Portuguese", async () => {
    mockApi({
      ...base(),
      "GET /v1/projects/p-1/ledger/entries?limit=25": () =>
        json(200, page([entry(), entry({ id: "je_2", recipe: "R8" }), entry({ id: "je_3", recipe: "R4" }), entry({ id: "je_4", recipe: "R3" })])),
      "GET /v1/projects/p-1/ledger/entries/je_3": () => json(200, detail({ id: "je_3", recipe: "R4" })),
      "GET /v1/projects/p-1/ledger/entries/je_4": () => json(200, detail({ id: "je_4", recipe: "R3" })),
    });
    setLocale("pt-BR");
    renderApp("/ledger?tab=entries");

    const table = await screen.findByRole("table", { name: "Lançamentos do diário" });
    expect(await within(table).findByText("Pagamento capturado")).toBeInTheDocument();
    expect(within(table).getByText("Estorno")).toBeInTheDocument();
    expect(within(table).getByText("Liquidação")).toBeInTheDocument();
    expect(within(table).getByText("Rateio")).toBeInTheDocument();
  });

  it("shows an entry whose debits equal its credits and links to the payment", async () => {
    mockApi(base());
    renderApp("/ledger/entries/je_1");

    expect(await screen.findByRole("heading", { name: "Journal entry" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Postings of the entry" });
    expect(within(table).getByTestId("total-debits")).toHaveTextContent("129.90");
    expect(within(table).getByTestId("total-credits")).toHaveTextContent("129.90");
    expect(within(table).getByText("Debits equal credits")).toBeInTheDocument();
    expect(within(table).getByText("Provider receivable")).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /Open payment pi_/ });
    // The payment flow posts with the intent id as correlation id, so that wins over metadata.payment_id.
    expect(link).toHaveAttribute("href", `/payments/${typedId("pi", "6a1f0c64-0000-4000-8000-000000000001")}`);
    expect(screen.getByText("capture:pay-1")).toBeInTheDocument();
    // Personal data the backend redacted is never shown as the raw marker.
    expect(screen.queryByText(/\[redacted\]/)).not.toBeInTheDocument();
  });

  it("explains a missing entry", async () => {
    mockApi({
      ...base(),
      "GET /v1/projects/p-1/ledger/entries/je_9": () => json(404, { title: "Not found", status: 404, detail: "no entry", errors: [] }),
    });
    renderApp("/ledger/entries/je_9");
    expect(await screen.findByText("Entry not found")).toBeInTheDocument();
  });
});
