import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCsrfToken } from "../../lib/api";
import { THEME_STORAGE_KEY } from "../../lib/theme";
import { expectNoAxeViolations } from "../../test/axe";
import { json, ME, mockApi, renderApp, resetApiState } from "../../test/harness";
import { NAV_ITEMS } from "./nav";

/** A session that ends once `endSession()` runs (what the API does after logout or expiry). */
function session() {
  let active = true;
  return {
    endSession: () => {
      active = false;
    },
    routes: {
      "GET /v1/auth/me": () => (active ? json(200, ME) : json(401, { title: "Unauthorized" })),
      "GET /health/live": () => json(200, { status: "live" }),
      "GET /health/ready": () => json(200, { status: "ready" }),
    },
  };
}

const signedIn = {
  "GET /v1/auth/me": () => json(200, ME),
  "GET /health/live": () => json(200, { status: "live" }),
  "GET /health/ready": () => json(200, { status: "ready" }),
};

describe("app shell", () => {
  beforeEach(() => {
    resetApiState();
    document.documentElement.removeAttribute("data-theme");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("has no axe violations and the expected landmarks", async () => {
    mockApi(signedIn);
    const { container } = renderApp("/");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();

    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getAllByRole("banner").length).toBeGreaterThan(0);
    expect(screen.getByRole("navigation", { name: /main|primary/i })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("lists every section in the sidebar and marks the current one", async () => {
    mockApi(signedIn);
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    const nav = screen.getByRole("navigation", { name: /main|primary/i });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent?.trim());
    expect(labels).toEqual(NAV_ITEMS.map((item) => item.label));
    expect(labels).toEqual([
      "Overview",
      "Projects",
      "Customers",
      "Ledger",
      "Payments",
      "Billing",
      "Integrations",
      "Events",
      "Webhooks",
      "Reconciliation",
      "Audit",
      "Settings",
    ]);
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Ledger" })).not.toHaveAttribute("aria-current");
  });

  it("renders a Planned placeholder for areas that are not built, without axe violations", async () => {
    mockApi(signedIn);
    const user = userEvent.setup();
    const { container } = renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.click(within(screen.getByRole("navigation", { name: /main|primary/i })).getByRole("link", { name: "Payments" }));
    expect(await screen.findByRole("heading", { name: "Payments" })).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(within(main).getByText("Planned")).toBeInTheDocument();
    expect(within(main).getByText(/isn't available in the console yet/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("switches between the operator's projects and remembers the choice", async () => {
    mockApi(signedIn);
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    const trigger = screen.getByRole("button", { name: /Project: Acme Payments/ });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitemradio", { name: /Beta Billing/ }));
    expect(await screen.findByRole("button", { name: /Project: Beta Billing/ })).toBeInTheDocument();
    expect(window.localStorage.getItem("fh.project")).toBe("p-2");
  });

  it("toggles and persists the theme", async () => {
    mockApi(signedIn);
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.click(screen.getByRole("button", { name: /Theme:/ }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(await screen.findByRole("button", { name: "Theme: Dark" })).toBeInTheDocument();
  });

  it("signs out with the CSRF header and returns to the login page", async () => {
    let csrfSeen: string | undefined;
    const { endSession, routes } = session();
    mockApi({
      ...routes,
      "POST /v1/auth/logout": ({ init }) => {
        csrfSeen = (init.headers as Record<string, string>)["X-CSRF-Token"];
        endSession();
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    const { router } = renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.click(screen.getByRole("button", { name: /Operator menu: Ana Souza/ }));
    expect(await screen.findByText("ana@example.test")).toBeInTheDocument();
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(csrfSeen).toBe("csrf-from-me");
    expect(getCsrfToken()).toBeNull();
  });

  it("reports a failed sign-out instead of leaving", async () => {
    mockApi({ ...signedIn, "POST /v1/auth/logout": () => json(500, { title: "Server error" }) });
    const user = userEvent.setup();
    const { router } = renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.click(screen.getByRole("button", { name: /Operator menu/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    expect(await screen.findByText("Couldn't sign out")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("redirects to /login when the session expires mid-use (401 on any call)", async () => {
    const { endSession, routes } = session();
    mockApi({
      ...routes,
      "POST /v1/auth/logout": () => {
        endSession();
        return json(401, { title: "Unauthorized" });
      },
    });
    const user = userEvent.setup();
    const { router } = renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.click(screen.getByRole("button", { name: /Operator menu/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });

  it("is reachable by keyboard: skip link first, then navigation, then the top bar", async () => {
    mockApi(signedIn);
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Overview" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("opens the Theme menu with the keyboard and closes it with Escape", async () => {
    mockApi(signedIn);
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });

    screen.getByRole("button", { name: /Theme:/ }).focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Theme:/ })).toHaveFocus();
  });
});
