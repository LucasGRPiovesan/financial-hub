import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const EVENT = {
  id: "ev-1",
  project_id: "p-1",
  type: "payment.confirmed",
  subject: "payment/pay-1",
  version: 1,
  occurred_at: "2026-10-10T12:00:00Z",
  recorded_at: "2026-10-10T12:00:01Z",
  correlation_id: "corr-1",
  causation_id: null,
  traceparent: null,
  txid: 4711,
  data: {},
};
const delivery = (overrides: Record<string, unknown> = {}) => ({
  id: "d-1",
  kind: "webhook",
  handler: null,
  subscription_id: "sub-1",
  status: "failed",
  attempts: 3,
  last_error: "connection refused",
  next_attempt_at: null,
  ...overrides,
});
const RETRY = "POST /v1/projects/p-1/events/ev-1/deliveries/d-1/retry";

function detail(deliveries: unknown[], extra: Record<string, () => Response> = {}) {
  return {
    "GET /v1/auth/me": () => json(200, ME),
    ...overviewData("p-1"),
    "GET /v1/projects/p-1/events?limit=25": () => json(200, page([])),
    "GET /v1/projects/p-1/events/ev-1": () => json(200, { event: EVENT, deliveries }),
    "GET /v1/projects/p-1/events/ev-1/causality": () =>
      json(200, { root: EVENT, ancestors: [], descendants: [], journal_entries: [], node_count: 1, truncated: false }),
    ...extra,
  };
}

describe("Resend a webhook delivery", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("queues the delivery again, shows it pending and toasts", async () => {
    let queued = false;
    const { calls } = mockApi(
      detail([delivery()], {
        [RETRY]: () => {
          queued = true;
          return json(202, {});
        },
        "GET /v1/projects/p-1/events/ev-1": () =>
          json(200, { event: EVENT, deliveries: [delivery(queued ? { status: "pending", attempts: 0, last_error: null } : {})] }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/events/ev-1");
    await user.click(await screen.findByRole("button", { name: "Resend delivery d-1" }));
    expect((await screen.findAllByText("Delivery queued to be resent")).length).toBeGreaterThan(0);
    expect(calls.some((call) => call.key === RETRY)).toBe(true);
    const table = await screen.findByRole("table", { name: "Deliveries of this event" });
    await waitFor(() => expect(within(table).getByText("Pending")).toBeInTheDocument());
  });

  it("rolls back and says so when the API refuses", async () => {
    mockApi(detail([delivery()], { [RETRY]: () => problem(409, "Delivery already succeeded") }));
    const user = userEvent.setup();
    renderApp("/events/ev-1");
    await user.click(await screen.findByRole("button", { name: "Resend delivery d-1" }));
    expect((await screen.findAllByText("Couldn't resend the delivery")).length).toBeGreaterThan(0);
    const table = await screen.findByRole("table", { name: "Deliveries of this event" });
    expect(within(table).getByText("Failed")).toBeInTheDocument();
  });

  it("offers Resend only for webhook deliveries", async () => {
    mockApi(detail([delivery(), delivery({ id: "d-2", kind: "handler", handler: "ledger" })]));
    renderApp("/events/ev-1");
    expect(await screen.findByRole("button", { name: "Resend delivery d-1" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Resend delivery d-2" })).not.toBeInTheDocument();
  });

  it("disables Resend for roles without webhooks:manage", async () => {
    const finance = { ...ME, memberships: [{ project_id: "p-1", project_name: "Acme Payments", role: "finance" }] };
    mockApi(detail([delivery()], { "GET /v1/auth/me": () => json(200, finance) }));
    renderApp("/events/ev-1");
    expect(await screen.findByRole("button", { name: "Resend delivery d-1" })).toBeDisabled();
  });
});
