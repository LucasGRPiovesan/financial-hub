import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { customer, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";
import { payment } from "../test/payment-mocks";

const LIST = "GET /v1/projects/p-1/payments?limit=25";
const DETAIL = "GET /v1/projects/p-1/payments/pay-1";
const REFUND = "POST /v1/projects/p-1/payments/pay-1/refunds";

const SETTLED = payment();
const PENDING = payment({
  id: "pay-2",
  customer_id: "c-2",
  method: "boleto",
  amount: 45000,
  status: "requires_payment",
  capture_status: "pending",
  settlement_status: "none",
  captured_amount: 0,
  refundable_amount: 0,
  external_ref: "order-2",
});
const PARTIAL = payment({
  id: "pay-3",
  method: "card",
  amount: 24900,
  status: "partially_refunded",
  refund_status: "partial",
  captured_amount: 24900,
  refunded_amount: 5000,
  refundable_amount: 19900,
});

const meAs = (role: string) => ({
  ...ME,
  memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]],
});

function base(role = "owner", rows: unknown[] = [SETTLED, PENDING, PARTIAL]) {
  return {
    "GET /v1/auth/me": () => json(200, meAs(role)),
    ...overviewData("p-1"),
    [LIST]: () => json(200, page(rows)),
    "GET /v1/projects/p-1/customers/c-1": () => json(200, customer()),
    "GET /v1/projects/p-1/customers/c-2": () => json(200, customer({ id: "c-2", name: "Padaria Estrela", external_ref: "cust_2" })),
  };
}

describe("Payments list", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists payments with customer, method, amount and status, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/payments");

    expect(await screen.findByRole("heading", { name: "Payments" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Payments" });
    expect((await within(table).findAllByText("Marina Alves")).length).toBe(2);
    expect(within(table).getByText("Padaria Estrela")).toBeInTheDocument();
    expect(within(table).getByText("PIX")).toBeInTheDocument();
    expect(within(table).getByText("Boleto")).toBeInTheDocument();
    expect(within(table).getByText("Card")).toBeInTheDocument();
    expect(within(table).getByText(/129.90/)).toBeInTheDocument();
    expect(within(table).getByText("Settled")).toBeInTheDocument();
    expect(within(table).getByText("Pending")).toBeInTheDocument();
    expect(within(table).getByText("Partially refunded")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Open payment pay-1" })).toBeInTheDocument();
    expect(screen.getByText("3 payments loaded")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("loads the next page with the cursor", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      ...base(),
      [LIST]: () => json(200, { data: [SETTLED], has_more: true, next_cursor: "cur-1" }),
      "GET /v1/projects/p-1/payments?limit=25&cursor=cur-1": () => json(200, page([PENDING])),
    });
    renderApp("/payments");
    expect(await screen.findByText("1 payment loaded, more available")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("link", { name: "Open payment pay-2" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open payment pay-1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(calls.some((call) => call.key.endsWith("&cursor=cur-1"))).toBe(true);
  });

  it("filters the loaded rows by status and method", async () => {
    const user = userEvent.setup();
    mockApi(base());
    renderApp("/payments");
    const table = await screen.findByRole("table", { name: "Payments" });
    await user.selectOptions(screen.getByLabelText("Method"), "boleto");
    expect(within(table).queryByText("PIX")).not.toBeInTheDocument();
    expect(within(table).getByText("Boleto")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Status"), "settled");
    expect(await screen.findByText("No payment matches")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]!);
    expect(await screen.findByRole("table", { name: "Payments" })).toBeInTheDocument();
  });

  it("shows the empty state and a retryable error", async () => {
    mockApi(base("owner", []));
    renderApp("/payments");
    expect(await screen.findByText("No payments yet")).toBeInTheDocument();
  });

  it("shows the problem message with a retry when loading fails", async () => {
    let fail = true;
    const user = userEvent.setup();
    mockApi({ ...base(), [LIST]: () => (fail ? problem(500, "Database is down") : json(200, page([SETTLED]))) });
    renderApp("/payments");
    expect(await screen.findByText("Database is down")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load payments")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Payments" })).toBeInTheDocument();
  });
});

describe("Payment detail and refund", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  function detailApi(role: string, extra: Record<string, () => Response> = {}) {
    return mockApi({ ...base(role), [DETAIL]: () => json(200, SETTLED), ...extra });
  }

  it("shows amounts, provider data and metadata, without axe violations", async () => {
    detailApi("owner");
    const { container } = renderApp("/payments/pay-1");
    expect(await screen.findByText("Charge amount")).toBeInTheDocument();
    expect(screen.getByText("pay_asaas_1")).toBeInTheDocument();
    expect(screen.getByText(/"channel": "web"/)).toBeInTheDocument();
    expect(screen.getByText("No refunds requested.")).toBeInTheDocument();
    expect(await screen.findByText("Marina Alves")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it.each(["viewer", "admin"])("disables the refund action for %s with a reason", async (role) => {
    detailApi(role);
    renderApp("/payments/pay-1");
    const button = await screen.findByRole("button", { name: "Refund payment" });
    expect(button).toBeDisabled();
    expect(button.closest("span")).toHaveAttribute("title", expect.stringMatching(/./));
  });

  it.each(["owner", "finance"])("lets %s refund", async (role) => {
    detailApi(role);
    renderApp("/payments/pay-1");
    expect(await screen.findByRole("button", { name: "Refund payment" })).toBeEnabled();
  });

  it("sends a partial refund as integer centavos with an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const { calls } = detailApi("owner", {
      [REFUND]: () => json(201, { id: "r-1", payment_id: "pay-1", amount: 1234, currency: "BRL", status: "requested", origin: "operator", reason: null, created_at: "2026-10-16T10:00:00Z", updated_at: "2026-10-16T10:00:00Z" }),
    });
    renderApp("/payments/pay-1");
    await user.click(await screen.findByRole("button", { name: "Refund payment" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Partial refund"));
    await user.type(within(dialog).getByLabelText("Amount to refund (R$)"), "12,34");
    await user.click(within(dialog).getByRole("button", { name: "Confirm refund" }));

    await waitFor(() => expect(calls.some((call) => call.key === REFUND)).toBe(true));
    const call = calls.find((entry) => entry.key === REFUND)!;
    const sent = { body: JSON.parse(String(call.init.body)), key: new Headers(call.init.headers).get("Idempotency-Key") };
    expect(sent.body).toEqual({ amount: 1234, reason: null });
    expect(sent.key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("rejects three decimals and amounts above the refundable maximum before sending", async () => {
    const user = userEvent.setup();
    const { calls } = detailApi("owner");
    renderApp("/payments/pay-1");
    await user.click(await screen.findByRole("button", { name: "Refund payment" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Partial refund"));
    const input = within(dialog).getByLabelText("Amount to refund (R$)");
    await user.type(input, "12,345");
    await user.click(within(dialog).getByRole("button", { name: "Confirm refund" }));
    expect(await within(dialog).findByText("Use at most two decimal places.")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "130,00");
    await user.click(within(dialog).getByRole("button", { name: "Confirm refund" }));
    expect(await within(dialog).findByText(/exceeds the refundable maximum/)).toBeInTheDocument();
    expect(calls.some((call) => call.key === REFUND)).toBe(false);
  });

  it("shows the server's problem when the refund is refused", async () => {
    const user = userEvent.setup();
    detailApi("owner", { [REFUND]: () => problem(409, "Payment is not refundable") });
    renderApp("/payments/pay-1");
    await user.click(await screen.findByRole("button", { name: "Refund payment" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Confirm refund" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Payment is not refundable");
  });
});
