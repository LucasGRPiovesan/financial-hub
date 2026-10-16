import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getUsageSummaryV1ProjectsProjectIdUsageSummaryGetUrl } from "../api/generated/endpoints/metering/metering";
import { expectNoAxeViolations } from "../test/axe";
import { overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

function metric(overrides: Record<string, unknown> = {}) {
  return {
    code: "api_calls",
    name: "API calls",
    description: null,
    aggregation: "count",
    field: null,
    status: "active",
    archived_at: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...overrides,
  };
}

const LIST = "GET /v1/projects/p-1/metrics?limit=100";
const meAs = (role: string) => ({ ...ME, memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]] });

function base(role = "owner", rows: unknown[] = [metric(), metric({ code: "bytes_out", name: "Bytes out", aggregation: "sum", field: "bytes" })]) {
  return { "GET /v1/auth/me": () => json(200, meAs(role)), ...overviewData("p-1"), [LIST]: () => json(200, page(rows)) };
}

describe("Billing: metrics", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists metrics and leaves room for what is coming, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/billing");
    const table = await screen.findByRole("table", { name: "Billable metrics" });
    expect(within(table).getByText("api_calls")).toBeInTheDocument();
    expect(within(table).getByText("Sum")).toBeInTheDocument();
    expect(within(table).getByText("bytes")).toBeInTheDocument();
    expect(screen.getByText("Plans, subscriptions and invoices")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Usage metering" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("validates the form before sending and requires a field unless the metric counts", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(base());
    renderApp("/billing");
    await user.click(await screen.findByRole("button", { name: "New metric" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Create metric" }));
    expect(within(dialog).getByText("Enter a code.")).toBeInTheDocument();
    expect(within(dialog).getByText("Enter a name.")).toBeInTheDocument();
    expect(within(dialog).getByText("Enter the event field to aggregate.")).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Code"), "Bad Code");
    await user.click(within(dialog).getByRole("button", { name: "Create metric" }));
    expect(within(dialog).getByText(/^Use lowercase letters, digits and underscores, starting with a letter \(up/)).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Aggregation"), "count");
    expect(within(dialog).getByLabelText("Event field")).toBeDisabled();
    expect(calls.some((call) => call.key.startsWith("POST"))).toBe(false);
  });

  it("creates a metric and refreshes the table", async () => {
    const user = userEvent.setup();
    let rows = [metric()];
    const { calls } = mockApi({
      ...base(),
      [LIST]: () => json(200, page(rows)),
      "POST /v1/projects/p-1/metrics": () => {
        rows = [...rows, metric({ code: "gb_stored", name: "GB stored", aggregation: "max", field: "gb" })];
        return json(201, rows[1]);
      },
    });
    renderApp("/billing");
    await user.click(await screen.findByRole("button", { name: "New metric" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "gb_stored");
    await user.type(within(dialog).getByLabelText("Name"), "GB stored");
    await user.selectOptions(within(dialog).getByLabelText("Aggregation"), "max");
    await user.type(within(dialog).getByLabelText("Event field"), "gb");
    await user.click(within(dialog).getByRole("button", { name: "Create metric" }));
    expect(await screen.findByText("gb_stored")).toBeInTheDocument();
    const post = calls.find((call) => call.key.startsWith("POST"));
    expect(JSON.parse(String(post?.init.body))).toMatchObject({ code: "gb_stored", name: "GB stored", aggregation: "max", field: "gb", description: null });
  });

  it("maps a server rejection onto the field", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base(),
      "POST /v1/projects/p-1/metrics": () => problem(422, "Invalid", [{ pointer: "/code", detail: "code already exists" }]),
    });
    renderApp("/billing");
    await user.click(await screen.findByRole("button", { name: "New metric" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Code"), "api_calls");
    await user.type(within(dialog).getByLabelText("Name"), "Dup");
    await user.selectOptions(within(dialog).getByLabelText("Aggregation"), "count");
    await user.click(within(dialog).getByRole("button", { name: "Create metric" }));
    expect(await within(dialog).findByText("code already exists")).toBeInTheDocument();
  });

  it("archives only after the code is typed", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      ...base(),
      "POST /v1/projects/p-1/metrics/api_calls/archive": () => json(200, metric({ status: "archived" })),
    });
    renderApp("/billing");
    await user.click(await screen.findByRole("button", { name: "Archive metric api_calls" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Archive metric" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Metric code"), "api_calls");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(calls.some((call) => call.key === "POST /v1/projects/p-1/metrics/api_calls/archive")).toBe(true));
  });

  it("disables creating and archiving for a viewer", async () => {
    mockApi(base("viewer"));
    renderApp("/billing");
    await screen.findByRole("table", { name: "Billable metrics" });
    expect(screen.getByRole("button", { name: "New metric" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Archive metric api_calls" })).toBeDisabled();
  });
});

describe("Billing: usage summary", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the exact decimal string, never a float", async () => {
    const user = userEvent.setup();
    const now = new Date();
    const summaryUrl = getUsageSummaryV1ProjectsProjectIdUsageSummaryGetUrl("p-1", {
      customer_id: "c-1",
      metric: "bytes_out",
      from: new Date(now.getFullYear(), now.getMonth(), 1).toISOString(),
      to: new Date(now.getFullYear(), now.getMonth() + 1, 1).toISOString(),
    });
    const { calls } = mockApi({
      ...base(),
      [`GET ${summaryUrl}`]: () =>
        json(200, {
          aggregation: "sum",
          customer_id: "c-1",
          end: "2026-11-01T00:00:00Z",
          events: 2,
          metric: "bytes_out",
          start: "2026-10-01T00:00:00Z",
          value: "12345678901234567890.500000000",
        }),
    });
    renderApp("/billing");
    await screen.findByRole("table", { name: "Billable metrics" });
    await user.selectOptions(screen.getByLabelText("Customer"), "c-1");
    await user.selectOptions(screen.getByLabelText("Metric"), "bytes_out");
    expect(await screen.findByTestId("usage-value")).toHaveTextContent("12,345,678,901,234,567,890.5");
    expect(screen.getByText("2 events counted")).toBeInTheDocument();
    expect(calls.some((call) => call.key === `GET ${summaryUrl}`)).toBe(true);
  });

  it("rejects an end before the start", async () => {
    const user = userEvent.setup();
    mockApi(base());
    renderApp("/billing");
    await screen.findByRole("table", { name: "Billable metrics" });
    const to = screen.getByLabelText("To (excluded)");
    await user.clear(to);
    await user.type(to, "2000-01-01");
    expect(await screen.findByRole("alert")).toHaveTextContent("The end must be after the start.");
  });
});
