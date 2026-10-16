import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

function event(overrides: Record<string, unknown> = {}) {
  return {
    id: "ev-1",
    project_id: "p-1",
    type: "payment.confirmed",
    subject: "payment/pay-1",
    version: 1,
    occurred_at: "2026-10-10T12:00:00Z",
    recorded_at: "2026-10-10T12:00:01Z",
    correlation_id: "corr-1",
    causation_id: null,
    traceparent: "00-abc-def-01",
    txid: 4711,
    data: { amount: 12990 },
    ...overrides,
  };
}

const LIST = "GET /v1/projects/p-1/events?limit=25";
const ev1 = event();
const ev2 = event({ id: "ev-2", type: "ledger.entry.posted", subject: "entry/e-1", causation_id: "ev-1" });

function base(rows: unknown[] = [ev1, ev2]) {
  return { "GET /v1/auth/me": () => json(200, ME), ...overviewData("p-1"), [LIST]: () => json(200, page(rows)) };
}

describe("Events list", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists events with humanized and raw types, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/events");
    const table = await screen.findByRole("table", { name: "Events, newest first" });
    expect(within(table).getByText("Payment confirmed")).toBeInTheDocument();
    expect(within(table).getByText("payment.confirmed")).toBeInTheDocument();
    expect(within(table).getByText("Ledger entry posted")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Open event ev-1" })).toHaveAttribute("href", "/events/ev-1");
    expect(screen.getByText("2 events loaded")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("filters by type", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      ...base(),
      "GET /v1/projects/p-1/events?limit=25&type=payment.confirmed": () => json(200, page([ev1])),
    });
    renderApp("/events");
    await screen.findByRole("table", { name: "Events, newest first" });
    await user.selectOptions(screen.getByLabelText("Event type"), "payment.confirmed");
    expect(await screen.findByText("1 event loaded")).toBeInTheDocument();
    expect(screen.queryByText("Ledger entry posted")).not.toBeInTheDocument();
    expect(calls.some((call) => call.key.includes("type=payment.confirmed"))).toBe(true);
  });

  it("shows an empty state", async () => {
    mockApi(base([]));
    renderApp("/events");
    expect(await screen.findByText("No events yet")).toBeInTheDocument();
  });

  it("explains a failed load", async () => {
    mockApi({ ...base(), [LIST]: () => problem(500, "Event log offline") });
    renderApp("/events");
    expect(await screen.findByText("Event log offline")).toBeInTheDocument();
  });
});

describe("Event detail", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  const delivery = (overrides: Record<string, unknown> = {}) => ({
    id: "d-1",
    kind: "webhook",
    handler: "webhooks",
    subscription_id: "sub-1",
    status: "failed",
    attempts: 3,
    last_error: "connection refused",
    next_attempt_at: null,
    ...overrides,
  });

  function detail(deliveries: unknown[] = [delivery()]) {
    return {
      ...base(),
      "GET /v1/projects/p-1/events/ev-1": () => json(200, { event: ev1, deliveries }),
      "GET /v1/projects/p-1/events/ev-1/causality": () =>
        json(200, {
          root: ev1,
          ancestors: [event({ id: "ev-0", type: "payment.created", subject: "payment/pay-1" })],
          descendants: [{ event: ev2, children: [] }],
          journal_entries: [{ id: "j-1", recipe: "payment_settled", status: "posted" }],
          node_count: 3,
          truncated: false,
        }),
    };
  }

  it("shows the envelope, the causality chain and the deliveries", async () => {
    mockApi(detail());
    const { container } = renderApp("/events/ev-1");
    expect(await screen.findByRole("heading", { name: "Event" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to events" })).toHaveAttribute("href", "/events");
    expect(screen.getByLabelText("Envelope", { selector: "pre" })).toHaveTextContent('"txid": 4711');
    expect(screen.getByRole("button", { name: "Copy Envelope" })).toBeInTheDocument();

    const deliveries = await screen.findByRole("table", { name: "Deliveries of this event" });
    expect(within(deliveries).getByText("sub-1")).toBeInTheDocument();
    expect(within(deliveries).getByText("Failed")).toBeInTheDocument();
    expect(within(deliveries).getByText("3")).toBeInTheDocument();
    expect(within(deliveries).getByText("connection refused")).toBeInTheDocument();

    const chain = await screen.findByRole("list", { name: "Causality" });
    const items = within(chain).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(within(items[0]!).getByText("Charge created")).toBeInTheDocument();
    expect(items[1]).toHaveAttribute("aria-current", "step");
    expect(within(items[2]!).getByRole("link", { name: "Open event ev-2" })).toHaveAttribute("href", "/events/ev-2");
    expect(screen.getByText("payment_settled")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("says when nothing was delivered", async () => {
    mockApi(detail([]));
    renderApp("/events/ev-1");
    expect(await screen.findByText("Nothing was delivered for this event.")).toBeInTheDocument();
  });

  it("explains an event that does not exist", async () => {
    mockApi({ ...detail(), "GET /v1/projects/p-1/events/ev-1": () => problem(404, "no such event") });
    renderApp("/events/ev-1");
    expect(await screen.findByText("Event not found")).toBeInTheDocument();
  });
});
