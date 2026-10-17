import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { LIST, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const SECRET = "whsec_SECRET_PLAINTEXT_0123456789";
const ENDPOINTS = `GET /v1/projects/p-1/webhook-endpoints${LIST}`;
const ENDPOINT = "/v1/projects/p-1/webhook-endpoints/we-1";
const ATTEMPTS = `GET ${ENDPOINT}/attempts?limit=50`;
const FINANCE_ME = { ...ME, memberships: [{ project_id: "p-1", project_name: "Acme Payments", role: "finance" }] };

function endpoint(overrides: Record<string, unknown> = {}) {
  return {
    id: "we-1",
    project_id: "p-1",
    url: "https://example.com/hooks",
    description: "Orders sink",
    event_types: ["payment.*", "ledger.entry.posted"],
    enabled: true,
    disabled_at: null,
    secret_rotation_ends_at: null,
    created_at: "2026-10-10T10:00:00Z",
    updated_at: "2026-10-10T10:00:00Z",
    ...overrides,
  };
}

function attempt(overrides: Record<string, unknown> = {}) {
  return {
    id: "at-1",
    delivery_id: "d-1",
    attempt: 1,
    outcome: "failed",
    status_code: 500,
    latency_ms: 123,
    error: "upstream returned 500 Internal Server Error with a very long explanation",
    attempted_at: "2026-10-10T12:00:00Z",
    ...overrides,
  };
}

const base = {
  "GET /v1/auth/me": () => json(200, ME),
  ...overviewData("p-1"),
};

describe("Webhooks list", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists endpoints with their event filter and status, without axe violations", async () => {
    mockApi({
      ...base,
      [ENDPOINTS]: () => json(200, page([endpoint(), endpoint({ id: "we-2", url: "https://other.example/in", description: null, event_types: ["*"], enabled: false })])),
    });
    const { container } = renderApp("/webhooks");
    const table = await screen.findByRole("table", { name: "Webhook endpoints" });
    const row = within(table).getByText("https://example.com/hooks").closest("tr") as HTMLElement;
    expect(within(row).getByText("payment.*")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    const other = within(table).getByText("https://other.example/in").closest("tr") as HTMLElement;
    expect(within(other).getByText("All events")).toBeInTheDocument();
    expect(within(other).getByText("Disabled")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Open endpoint https://example.com/hooks" })).toHaveAttribute("href", "/webhooks/we-1");
    await expectNoAxeViolations(container);
  });

  it("shows an empty state", async () => {
    mockApi({ ...base, [ENDPOINTS]: () => json(200, page([])) });
    renderApp("/webhooks");
    expect(await screen.findByText("No webhook endpoints yet")).toBeInTheDocument();
  });

  it("shows the problem message when the list fails", async () => {
    mockApi({ ...base, [ENDPOINTS]: () => problem(500, "Database is down") });
    renderApp("/webhooks");
    expect(await screen.findByText("Database is down")).toBeInTheDocument();
  });

  it("shows the secret once, blocks closing until confirmed, and keeps it out of every cache", async () => {
    let created = false;
    const { calls } = mockApi({
      ...base,
      [ENDPOINTS]: () => json(200, page(created ? [endpoint()] : [])),
      "POST /v1/projects/p-1/webhook-endpoints": () => {
        created = true;
        return json(201, { ...endpoint(), secret: SECRET });
      },
    });
    const { queryClient } = renderApp("/webhooks");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add endpoint" }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Endpoint URL" }), "https://example.com/hooks");
    await user.type(within(dialog).getByRole("textbox", { name: "Description" }), "Orders sink");
    await user.click(within(dialog).getByRole("button", { name: "Create endpoint" }));

    const shown = await within(await screen.findByRole("dialog")).findByLabelText("Signing secret");
    expect(shown).toHaveValue(SECRET);
    const post = calls.find((call) => call.key === "POST /v1/projects/p-1/webhook-endpoints");
    expect(JSON.parse(String(post?.init.body))).toMatchObject({ url: "https://example.com/hooks", description: "Orders sink", event_types: ["*"] });

    await user.keyboard("{Escape}");
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Signing secret")).toHaveValue(SECRET);
    expect(within(dialog).getByText(/shown only once/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Standard Webhooks/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "I saved it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(SECRET);

    const cached = JSON.stringify(
      [...queryClient.getQueryCache().getAll(), ...queryClient.getMutationCache().getAll()].map((entry) => ({
        key: entry.options,
        state: entry.state,
      })),
      (_key, value: unknown) => (typeof value === "function" ? undefined : value),
    );
    expect(cached).not.toContain(SECRET);
    expect(window.localStorage.getItem("fh.secret")).toBeNull();
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(SECRET);
    expect(await screen.findByText("https://example.com/hooks")).toBeInTheDocument();
  });

  it("shows the backend's URL validation message inline", async () => {
    mockApi({
      ...base,
      [ENDPOINTS]: () => json(200, page([])),
      "POST /v1/projects/p-1/webhook-endpoints": () =>
        problem(422, "Invalid", [{ pointer: "/url", detail: "Webhook URLs must use https" }]),
    });
    const user = userEvent.setup();
    renderApp("/webhooks");
    await user.click(await screen.findByRole("button", { name: "Add endpoint" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Endpoint URL" }), "http://example.com/hooks");
    await user.click(within(dialog).getByRole("button", { name: "Create endpoint" }));
    expect(await within(dialog).findByText("Webhook URLs must use https")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Endpoint URL" })).toHaveAttribute("aria-invalid", "true");
  });

  it("lets finance read endpoints but not create them", async () => {
    mockApi({ ...base, "GET /v1/auth/me": () => json(200, FINANCE_ME), [ENDPOINTS]: () => json(200, page([endpoint()])) });
    renderApp("/webhooks");
    expect(await screen.findByText("https://example.com/hooks")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add endpoint" })).toBeDisabled();
  });

  it("explains why a viewer has no access, without calling the API", async () => {
    window.localStorage.setItem("fh.project", "p-2");
    const { calls } = mockApi({ ...base });
    renderApp("/webhooks");
    expect(await screen.findByText("You can't see webhooks")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add endpoint" })).toBeDisabled();
    expect(calls.some((call) => call.key.includes("webhook-endpoints"))).toBe(false);
  });
});

describe("Webhook endpoint detail", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  const detail = (extra: Record<string, (...args: never[]) => Response | Promise<Response>> = {}) => ({
    ...base,
    [`GET ${ENDPOINT}`]: () => json(200, endpoint()),
    [ATTEMPTS]: () => json(200, { data: [attempt(), attempt({ id: "at-2", attempt: 2, outcome: "delivered", status_code: 200, latency_ms: 40, error: null })] }),
    ...extra,
  });

  it("renders the endpoint and the attempts table, without axe violations", async () => {
    mockApi(detail());
    const { container } = renderApp("/webhooks/we-1");
    expect(await screen.findByRole("heading", { name: "Webhook endpoint" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to webhooks" })).toHaveAttribute("href", "/webhooks");
    const table = await screen.findByRole("table", { name: "Delivery attempts, newest first" });
    const failed = within(table).getByText("Failed").closest("tr") as HTMLElement;
    expect(within(failed).getByText("500")).toBeInTheDocument();
    expect(within(failed).getByText("123 ms")).toBeInTheDocument();
    expect(within(failed).getByText(/upstream returned 500/)).toHaveAttribute("title", expect.stringContaining("upstream returned 500"));
    const delivered = within(table).getByText("Delivered").closest("tr") as HTMLElement;
    expect(within(delivered).getByText("200")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("refreshes the attempts on demand", async () => {
    let calls = 0;
    mockApi(
      detail({
        [ATTEMPTS]: () => {
          calls += 1;
          return json(200, { data: calls > 1 ? [attempt({ id: "at-9", attempt: 9 })] : [] });
        },
      }),
    );
    const user = userEvent.setup();
    renderApp("/webhooks/we-1");
    expect(await screen.findByText(/No attempts yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("table", { name: "Delivery attempts, newest first" })).toBeInTheDocument();
  });

  it("explains an endpoint that does not exist", async () => {
    mockApi(detail({ [`GET ${ENDPOINT}`]: () => problem(404, "no such endpoint") }));
    renderApp("/webhooks/we-1");
    expect(await screen.findByText("Endpoint not found")).toBeInTheDocument();
  });

  it("rotates the secret only after confirmation and reveals the new one once", async () => {
    const { calls } = mockApi(
      detail({
        [`POST ${ENDPOINT}/rotate-secret`]: () => json(200, { ...endpoint({ secret_rotation_ends_at: "2026-10-17T12:00:00Z" }), secret: SECRET }),
      }),
    );
    const { queryClient } = renderApp("/webhooks/we-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Rotate secret" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/24 hours/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(calls.some((call) => call.key.includes("rotate-secret"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Rotate secret" }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Rotate secret" }));
    expect(await within(await screen.findByRole("dialog")).findByLabelText("Signing secret")).toHaveValue(SECRET);

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "I saved it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(SECRET);
    const cached = JSON.stringify(
      [...queryClient.getQueryCache().getAll(), ...queryClient.getMutationCache().getAll()].map((entry) => ({ key: entry.options, state: entry.state })),
      (_key, value: unknown) => (typeof value === "function" ? undefined : value),
    );
    expect(cached).not.toContain(SECRET);
  });

  it("asks before disabling an endpoint", async () => {
    let disabled = false;
    const { calls } = mockApi(
      detail({
        [`GET ${ENDPOINT}`]: () => json(200, endpoint({ enabled: !disabled })),
        [`POST ${ENDPOINT}/disable`]: () => {
          disabled = true;
          return json(200, endpoint({ enabled: false }));
        },
      }),
    );
    const user = userEvent.setup();
    renderApp("/webhooks/we-1");
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(calls.some((call) => call.key.includes("/disable"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Disable" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disable endpoint" }));
    expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
  });

  it("sends a test event, toasts, and links to the new event", async () => {
    mockApi(detail({ [`POST ${ENDPOINT}/test`]: () => json(202, { event_id: "ev-77" }) }));
    const user = userEvent.setup();
    renderApp("/webhooks/we-1");
    await user.click(await screen.findByRole("button", { name: "Send test event" }));
    expect((await screen.findAllByText("Test event sent")).length).toBeGreaterThan(0);
    expect(await screen.findByRole("link", { name: "View event" })).toHaveAttribute("href", "/events/ev-77");
  });

  it("keeps the actions disabled for finance", async () => {
    mockApi(detail({ "GET /v1/auth/me": () => json(200, FINANCE_ME) }));
    renderApp("/webhooks/we-1");
    expect(await screen.findByRole("table", { name: "Delivery attempts, newest first" })).toBeInTheDocument();
    for (const name of ["Edit", "Rotate secret", "Disable", "Send test event"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });
});
