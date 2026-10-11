import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCsrfToken } from "../../lib/api";
import { expectNoAxeViolations } from "../../test/axe";
import { json, ME, mockApi, renderApp, resetApiState } from "../../test/harness";

const unauthenticated = () => json(401, { type: "about:blank", title: "Unauthorized", status: 401 });

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, password = "correct horse") {
  await user.type(await screen.findByLabelText("Email"), "ana@example.test");
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("login page", () => {
  beforeEach(resetApiState);
  afterEach(() => vi.unstubAllGlobals());

  it("has no axe violations and a labelled form", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated });
    const { container } = renderApp("/login");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    await expectNoAxeViolations(container);
  });

  it("signs in, keeps the CSRF token in memory and lands on the overview", async () => {
    let signedIn = false;
    const { calls } = mockApi({
      "GET /v1/auth/me": () => (signedIn ? json(200, ME) : unauthenticated()),
      "POST /v1/auth/login": ({ init }) => {
        signedIn = true;
        expect(JSON.parse(String(init.body))).toEqual({ email: "ana@example.test", password: "correct horse" });
        return json(200, { operator: ME.operator, csrf_token: "csrf-from-login" });
      },
      "GET /health/live": () => json(200, { status: "live" }),
      "GET /health/ready": () => json(200, { status: "ready" }),
    });
    const user = userEvent.setup();
    const { router } = renderApp("/login");
    await fillAndSubmit(user);

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(getCsrfToken()).toBe("csrf-from-me");
    expect(JSON.stringify(window.localStorage)).not.toContain("csrf");
    expect(calls.filter((call) => call.key === "POST /v1/auth/login")).toHaveLength(1);
  });

  it("returns to the page that was requested before sign-in", async () => {
    let signedIn = false;
    mockApi({
      "GET /v1/auth/me": () => (signedIn ? json(200, ME) : unauthenticated()),
      "POST /v1/auth/login": () => {
        signedIn = true;
        return json(200, { operator: ME.operator, csrf_token: "t" });
      },
    });
    const user = userEvent.setup();
    const { router } = renderApp("/ledger");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toEqual({ redirect: "/ledger" });

    await fillAndSubmit(user);
    await waitFor(() => expect(router.state.location.pathname).toBe("/ledger"));
    expect(await screen.findByRole("heading", { name: "Ledger" })).toBeInTheDocument();
  });

  it("shows a credentials error without leaking which field was wrong", async () => {
    mockApi({
      "GET /v1/auth/me": unauthenticated,
      "POST /v1/auth/login": () => json(401, { title: "Invalid credentials" }),
    });
    const user = userEvent.setup();
    const { container } = renderApp("/login");
    await fillAndSubmit(user, "wrong");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The email or password is incorrect.");
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    await expectNoAxeViolations(container);
  });

  it("locks the form for Retry-After seconds after a 429, then re-enables it", async () => {
    mockApi({
      "GET /v1/auth/me": unauthenticated,
      "POST /v1/auth/login": () => json(429, { title: "Too many attempts" }, { "Retry-After": "2" }),
    });
    const user = userEvent.setup();
    const { container } = renderApp("/login");
    await fillAndSubmit(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many sign-in attempts/);
    const button = screen.getByRole("button", { name: /Try again in 2s/ });
    expect(button).toBeDisabled();
    await expectNoAxeViolations(container);

    expect(await screen.findByRole("button", { name: "Sign in" }, { timeout: 4000 })).toBeEnabled();
  });

  it("explains a network failure", async () => {
    mockApi({
      "GET /v1/auth/me": unauthenticated,
      "POST /v1/auth/login": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const user = userEvent.setup();
    renderApp("/login");
    await fillAndSubmit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Can't reach the Financial Hub API/);
  });

  it("is operable by keyboard alone, with visible focus styles on the controls", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated });
    const user = userEvent.setup();
    renderApp("/login");
    await screen.findByRole("heading", { name: "Sign in" });

    await user.tab();
    expect(screen.getByLabelText("Email")).toHaveFocus();
    await user.tab();
    expect(screen.getByLabelText("Password")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Sign in" })).toHaveFocus();
  });

  it("skips the form when already signed in", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, ME), "GET /health/live": () => json(200, {}) });
    const { router } = renderApp("/login");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});
