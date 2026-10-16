import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { apiKey, LIST, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const KEYS = `GET /v1/projects/p-1/api-keys${LIST}`;
const PLAINTEXT = "fh_test_SECRET_PLAINTEXT_0123456789";
const base = {
  "GET /v1/auth/me": () => json(200, ME),
  ...overviewData("p-1"),
};

describe("API keys screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists keys with prefix, scopes and status, without axe violations", async () => {
    mockApi({
      ...base,
      [KEYS]: () => json(200, page([apiKey(), apiKey({ id: "k-2", name: "Old", status: "revoked" })])),
    });
    const { container } = renderApp("/api-keys");
    const row = (await screen.findByText("Server key")).closest("tr") as HTMLElement;
    expect(within(row).getByText("fh_test_ab12")).toBeInTheDocument();
    expect(within(row).getByText("ledger:read")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke Server key" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Revoke Old" })).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows an empty state", async () => {
    mockApi({ ...base, [KEYS]: () => json(200, page([])) });
    renderApp("/api-keys");
    expect(await screen.findByText("No API keys yet")).toBeInTheDocument();
  });

  it("shows the problem message and retries", async () => {
    let fail = true;
    mockApi({ ...base, [KEYS]: () => (fail ? problem(500, "Database is down") : json(200, page([apiKey()]))) });
    const user = userEvent.setup();
    renderApp("/api-keys");
    expect(await screen.findByText("Database is down")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Server key")).toBeInTheDocument();
  });

  it("maps field errors from the create request onto the fields", async () => {
    mockApi({
      ...base,
      [KEYS]: () => json(200, page([])),
      "POST /v1/projects/p-1/api-keys": () => problem(422, "Invalid", [{ pointer: "/name", detail: "Name is required" }]),
    });
    const user = userEvent.setup();
    renderApp("/api-keys");
    await user.click(await screen.findByRole("button", { name: "Create API key" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Create key" }));
    expect(await within(dialog).findByText("Name is required")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveAttribute("aria-invalid", "true");
  });

  it("shows the plaintext once, blocks closing until confirmed, and keeps it out of every cache", async () => {
    let created = false;
    const { calls, queryClient } = {
      ...mockApi({
        ...base,
        [KEYS]: () => json(200, page(created ? [apiKey()] : [])),
        "POST /v1/projects/p-1/api-keys": () => {
          created = true;
          return json(201, { ...apiKey(), key: PLAINTEXT });
        },
      }),
      ...renderApp("/api-keys"),
    };
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Create API key" }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Server key");
    await user.click(within(dialog).getByRole("checkbox", { name: /ledger:read/ }));
    await user.click(within(dialog).getByRole("button", { name: "Create key" }));

    const shown = await within(await screen.findByRole("dialog")).findByLabelText("API key");
    expect(shown).toHaveValue(PLAINTEXT);
    const post = calls.find((call) => call.key === "POST /v1/projects/p-1/api-keys");
    expect(JSON.parse(String(post?.init.body))).toMatchObject({ name: "Server key", scopes: ["ledger:read"] });

    // Escape cannot dismiss it; the operator is told why.
    await user.keyboard("{Escape}");
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/can't be shown again/);
    expect(within(dialog).getByLabelText("API key")).toHaveValue(PLAINTEXT);

    await user.click(within(dialog).getByRole("button", { name: "I stored it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(PLAINTEXT);

    const cached = JSON.stringify(
      [...queryClient.getQueryCache().getAll(), ...queryClient.getMutationCache().getAll()].map((entry) => ({
        key: entry.options,
        state: entry.state,
      })),
      (_key, value: unknown) => (typeof value === "function" ? undefined : value),
    );
    expect(cached).not.toContain(PLAINTEXT);
    expect(await screen.findByText("Server key")).toBeInTheDocument();
  });

  it("asks for confirmation before revoking, then refreshes the list", async () => {
    let revoked = false;
    const { calls } = mockApi({
      ...base,
      [KEYS]: () => json(200, page([apiKey({ status: revoked ? "revoked" : "active" })])),
      "POST /v1/projects/p-1/api-keys/k-1/revoke": () => {
        revoked = true;
        return json(200, apiKey({ status: "revoked" }));
      },
    });
    const user = userEvent.setup();
    renderApp("/api-keys");
    await user.click(await screen.findByRole("button", { name: "Revoke Server key" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(calls.some((call) => call.key.includes("/revoke"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Revoke Server key" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Revoke Server key" })).not.toBeInTheDocument());
    expect(screen.getByText("Revoked")).toBeInTheDocument();
  });

  it("hides key management from roles that cannot manage keys", async () => {
    window.localStorage.setItem("fh.project", "p-2");
    const { calls } = mockApi({ ...base });
    renderApp("/api-keys");
    expect(await screen.findByText("API keys aren't available for your role")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create API key" })).toBeDisabled();
    expect(calls.some((call) => call.key.includes("api-keys"))).toBe(false);
  });
});
