import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { CATALOG, LIST, overviewData, page, problem, providerAccount } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const ACCOUNTS = `GET /v1/projects/p-1/provider-accounts${LIST}`;
const base = {
  "GET /v1/auth/me": () => json(200, ME),
  ...overviewData("p-1"),
  "GET /v1/provider-catalog": () => json(200, CATALOG),
};

describe("Provider accounts screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists accounts, shows other providers as coming soon, and never shows secrets", async () => {
    mockApi({ ...base, [ACCOUNTS]: () => json(200, page([providerAccount()])) });
    const { container } = renderApp("/integrations");
    const row = (await screen.findByText("Main Asaas")).closest("tr") as HTMLElement;
    expect(within(row).getByText("sandbox")).toBeInTheDocument();
    expect(within(row).getByText("active")).toBeInTheDocument();
    expect(within(row).getByText(/9f2c/)).toBeInTheDocument();
    expect(await screen.findByText(/stripe — coming soon/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows an empty state and an error state with retry", async () => {
    let fail = false;
    mockApi({ ...base, [ACCOUNTS]: () => (fail ? problem(500, "Providers down") : json(200, page([]))) });
    const { queryClient } = renderApp("/integrations");
    expect(await screen.findByText("No provider accounts yet")).toBeInTheDocument();
    fail = true;
    await queryClient.invalidateQueries();
    expect(await screen.findByText("Providers down")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("creates an account with password inputs and maps credential errors to the field", async () => {
    mockApi({
      ...base,
      [ACCOUNTS]: () => json(200, page([])),
      "POST /v1/projects/p-1/provider-accounts": () =>
        problem(422, "Invalid", [{ pointer: "/credentials/api_key", detail: "API key rejected" }]),
    });
    const user = userEvent.setup();
    renderApp("/integrations");
    // Enabled once the catalog has loaded (the button is re-created when it flips, so query it again).
    await waitFor(() => expect(screen.getByRole("button", { name: "Add provider account" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Add provider account" }));
    const dialog = await screen.findByRole("dialog");
    const secret = within(dialog).getByLabelText("Api key");
    expect(secret).toHaveAttribute("type", "password");
    expect(secret).toHaveValue("");
    await user.type(within(dialog).getByRole("textbox", { name: "Label" }), "Main");
    await user.type(secret, "bad");
    await user.click(within(dialog).getByRole("button", { name: "Add account" }));
    expect(await within(dialog).findByText("API key rejected")).toBeInTheDocument();
    expect(secret).toHaveAttribute("aria-invalid", "true");
  });

  it("updates credentials from an empty password field", async () => {
    const { calls } = mockApi({
      ...base,
      [ACCOUNTS]: () => json(200, page([providerAccount()])),
      "PUT /v1/projects/p-1/provider-accounts/pa-1/credentials": () => json(200, providerAccount()),
    });
    const user = userEvent.setup();
    renderApp("/integrations");
    await user.click(await screen.findByRole("button", { name: "Update credentials for Main Asaas" }));
    const dialog = await screen.findByRole("dialog");
    const secret = within(dialog).getByLabelText("Api key");
    expect(secret).toHaveValue("");
    await user.type(secret, "new-secret");
    await user.click(within(dialog).getByRole("button", { name: "Replace credentials" }));
    await waitFor(() => expect(calls.some((call) => call.key.startsWith("PUT "))).toBe(true));
    const put = calls.find((call) => call.key.startsWith("PUT "));
    expect(JSON.parse(String(put?.init.body))).toEqual({ credentials: { api_key: "new-secret" } });
  });

  it("confirms before disabling", async () => {
    const { calls } = mockApi({
      ...base,
      [ACCOUNTS]: () => json(200, page([providerAccount()])),
      "POST /v1/projects/p-1/provider-accounts/pa-1/deactivate": () => json(200, providerAccount({ status: "disabled" })),
    });
    const user = userEvent.setup();
    renderApp("/integrations");
    await user.click(await screen.findByRole("button", { name: "Disable Main Asaas" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(calls.some((call) => call.key.includes("deactivate"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Disable Main Asaas" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disable account" }));
    await waitFor(() => expect(calls.some((call) => call.key.includes("deactivate"))).toBe(true));
  });

  it("disables management controls for read-only roles", async () => {
    window.localStorage.setItem("fh.project", "p-2");
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      "GET /v1/provider-catalog": () => json(200, CATALOG),
      [`GET /v1/projects/p-2/provider-accounts${LIST}`]: () => json(200, page([providerAccount({ project_id: "p-2" })])),
    });
    renderApp("/integrations");
    expect(await screen.findByRole("button", { name: "Disable Main Asaas" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add provider account" })).toBeDisabled();
  });
});
