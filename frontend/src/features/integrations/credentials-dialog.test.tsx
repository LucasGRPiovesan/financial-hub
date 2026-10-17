import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CATALOG, LIST, overviewData, page, providerAccount } from "../../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../../test/harness";
import { WEBHOOK_TOKEN_PATTERN, generateWebhookToken } from "./webhook-credentials";

const base = {
  "GET /v1/auth/me": () => json(200, ME),
  ...overviewData("p-1"),
  "GET /v1/provider-catalog": () => json(200, CATALOG),
  [`GET /v1/projects/p-1/provider-accounts${LIST}`]: () => json(200, page([providerAccount()])),
};

describe("generateWebhookToken", () => {
  it("makes 48 alphanumeric characters that differ every time", () => {
    const tokens = new Set(Array.from({ length: 20 }, generateWebhookToken));
    expect(tokens.size).toBe(20);
    for (const token of tokens) expect(token).toMatch(WEBHOOK_TOKEN_PATTERN);
  });
});

describe("Credentials dialog", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  async function openDialog() {
    const user = userEvent.setup();
    renderApp("/integrations");
    await user.click(await screen.findByRole("button", { name: "Update credentials for Main Asaas" }));
    return { user, dialog: await screen.findByRole("dialog") };
  }

  it("warns that saving replaces the whole set and never prefills secrets", async () => {
    mockApi(base);
    const { dialog } = await openDialog();
    expect(within(dialog).getByTestId("replace-warning")).toHaveTextContent(/replaces the whole credential set/);
    expect(within(dialog).getByLabelText("API key")).toHaveAttribute("type", "password");
    expect(within(dialog).getByLabelText("API key")).toHaveValue("");
  });

  it("generates a 48-character token, shows the webhook URL and sends both", async () => {
    const { calls } = mockApi({
      ...base,
      "PUT /v1/projects/p-1/provider-accounts/pa-1/credentials": () => json(200, { id: "pa-1" }),
    });
    const { user, dialog } = await openDialog();
    const url = within(dialog).getByLabelText("Webhook URL");
    expect(url).toHaveAttribute("readonly");
    expect(url).toHaveValue(`${window.location.origin}/v1/webhooks/asaas/pa-1`);
    expect(within(dialog).getByText(/asaas-access-token/)).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("API key"), "new-secret");
    await user.click(within(dialog).getByRole("button", { name: "Generate token" }));
    const token = (within(dialog).getByLabelText("Webhook token (optional)") as HTMLInputElement).value;
    expect(token).toMatch(WEBHOOK_TOKEN_PATTERN);
    expect(within(dialog).getByRole("button", { name: "Copy Webhook token" })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Replace credentials" }));
    const put = calls.find((call) => call.key.startsWith("PUT"));
    expect(JSON.parse(String(put?.init.body))).toEqual({ credentials: { api_key: "new-secret", webhook_token: token } });
  });
});
