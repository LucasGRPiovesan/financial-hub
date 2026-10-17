import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { version } from "../../package.json";
import { expectNoAxeViolations } from "../test/axe";
import { overviewData } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const base = { "GET /v1/auth/me": () => json(200, ME), ...overviewData("p-1") };

describe("Settings", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the profile, project and build, without axe violations", async () => {
    mockApi(base);
    const { container } = renderApp("/settings");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByText("Ana Souza", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("ana@example.test", { selector: "dd span" })).toBeInTheDocument();
    expect(screen.getByText("Acme Payments", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("p-1")).toBeInTheDocument();
    expect(screen.getByText("Sandbox")).toBeInTheDocument();
    expect(screen.getByText(`v${version}`)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "README" })).toHaveAttribute("href", expect.stringContaining("github.com"));
    await expectNoAxeViolations(container);
  });

  it("switches language and theme", async () => {
    mockApi(base);
    const user = userEvent.setup();
    renderApp("/settings");
    const language = await screen.findByRole("radiogroup", { name: "Language" });
    expect(within(language).getByRole("radio", { name: "English" })).toBeChecked();
    await user.click(within(language).getByRole("radio", { name: "Português" }));
    expect(await screen.findByRole("heading", { name: "Configurações" })).toBeInTheDocument();

    const theme = screen.getByRole("radiogroup", { name: "Tema" });
    await user.click(within(theme).getByRole("radio", { name: "Escuro" }));
    expect(within(theme).getByRole("radio", { name: "Escuro" })).toBeChecked();
  });
});
