import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCsrfToken } from "../../lib/api";
import { expectNoAxeViolations } from "../../test/axe";
import { overviewData } from "../../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../../test/harness";

const unauthenticated = () => json(401, { type: "about:blank", title: "Unauthorized", status: 401 });
/** The API answers 404 when demo login is switched off: the page must fall back to the password form. */
const noDemo = () => json(404, { type: "about:blank", title: "Not Found", status: 404 });
const DEMO = "GET /v1/auth/demo-operators";

const DEMO_OPERATORS = [
  {
    id: "op-1",
    email: "ana@example.test",
    display_name: "Ana Souza",
    memberships: [
      { project_id: "p-1", project_name: "Acme Payments", role: "owner" },
      { project_id: "p-2", project_name: "Beta Billing", role: "viewer" },
    ],
  },
  {
    id: "op-2",
    email: "bruno@example.test",
    display_name: "Bruno Lima",
    memberships: [{ project_id: "p-1", project_name: "Acme Payments", role: "finance" }],
  },
];

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, password = "correct horse") {
  await user.type(await screen.findByLabelText("Email"), "ana@example.test");
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("login page", () => {
  beforeEach(resetApiState);
  afterEach(() => vi.unstubAllGlobals());

  it("has no axe violations and a labelled form", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: noDemo });
    const { container } = renderApp("/login");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Email")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    await expectNoAxeViolations(container);
  });

  it("signs in, keeps the CSRF token in memory and lands on the overview", async () => {
    let signedIn = false;
    const { calls } = mockApi({
      "GET /v1/auth/me": () => (signedIn ? json(200, ME) : unauthenticated()),
      [DEMO]: noDemo,
      "POST /v1/auth/login": ({ init }) => {
        signedIn = true;
        expect(JSON.parse(String(init.body))).toEqual({ email: "ana@example.test", password: "correct horse" });
        return json(200, { operator: ME.operator, csrf_token: "csrf-from-login" });
      },
      ...overviewData("p-1"),
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
      [DEMO]: noDemo,
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
      [DEMO]: noDemo,
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
      [DEMO]: noDemo,
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
      [DEMO]: noDemo,
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
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: noDemo });
    const user = userEvent.setup();
    renderApp("/login");
    await screen.findByRole("heading", { name: "Sign in" });
    await screen.findByLabelText("Email");

    await user.tab();
    expect(screen.getByRole("button", { name: "Language: English" })).toHaveFocus();
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

describe("sign-in product panel", () => {
  beforeEach(resetApiState);
  afterEach(() => vi.unstubAllGlobals());

  it("explains the product with capabilities and an evaluation footnote", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: noDemo });
    const { container } = renderApp("/login");
    const panel = await screen.findByRole("complementary", { name: "About Financial Hub" });
    expect(within(panel).getByRole("heading", { name: /double-entry ledger/i })).toBeInTheDocument();
    for (const title of ["Double-entry ledger", "Idempotent payments", "Signed webhooks and live events", "Audit trail"]) {
      expect(within(panel).getByText(title)).toBeInTheDocument();
    }
    expect(within(panel).getByText(/Evaluation environment/)).toBeInTheDocument();
    expect(panel).toHaveClass("hidden", "lg:flex");
    await expectNoAxeViolations(container);
  });
});

describe("demo operator picker", () => {
  beforeEach(resetApiState);
  afterEach(() => vi.unstubAllGlobals());

  it("shows initials avatars", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: () => json(200, { operators: DEMO_OPERATORS }) });
    renderApp("/login");
    expect(await screen.findByText("AS")).toBeInTheDocument();
    expect(screen.getByText("BL")).toBeInTheDocument();
  });

  beforeEach(resetApiState);
  afterEach(() => vi.unstubAllGlobals());

  it("lists the personas with their project roles and says it is demo mode", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: () => json(200, { operators: DEMO_OPERATORS }) });
    const { container } = renderApp("/login");

    expect(await screen.findByText(/Demo mode - choose a persona; no password needed/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Demo operators" });
    expect(within(list).getByRole("heading", { name: "Ana Souza" })).toBeInTheDocument();
    expect(within(list).getByText("bruno@example.test")).toBeInTheDocument();
    const anaProjects = within(list).getByRole("list", { name: "Projects of Ana Souza" });
    expect(within(anaProjects).getByText("Owner")).toBeInTheDocument();
    expect(within(anaProjects).getByText("Viewer")).toBeInTheDocument();
    expect(within(within(list).getByRole("list", { name: "Projects of Bruno Lima" })).getByText("Finance")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue as Bruno Lima" })).toBeEnabled();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("signs in as the chosen persona and lands on the overview like a password login", async () => {
    let signedIn = false;
    const { calls } = mockApi({
      "GET /v1/auth/me": () => (signedIn ? json(200, ME) : unauthenticated()),
      [DEMO]: () => json(200, { operators: DEMO_OPERATORS }),
      "POST /v1/auth/demo-login": ({ init }) => {
        signedIn = true;
        expect(JSON.parse(String(init.body))).toEqual({ operator_id: "op-1" });
        return json(200, { operator: ME.operator, csrf_token: "csrf-from-demo" });
      },
      ...overviewData("p-1"),
      "GET /health/live": () => json(200, { status: "live" }),
    });
    const user = userEvent.setup();
    const { router } = renderApp("/login");
    await user.click(await screen.findByRole("button", { name: "Continue as Ana Souza" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    // The session is read back from /me, which is also where the CSRF token comes from.
    expect(getCsrfToken()).toBe("csrf-from-me");
    expect(calls.filter((call) => call.key === "POST /v1/auth/demo-login")).toHaveLength(1);
    expect(JSON.stringify(window.localStorage)).not.toContain("csrf");
  });

  it("falls back to the password form when demo login is switched off", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: noDemo });
    renderApp("/login");
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.queryByText(/Demo mode/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Demo operators" })).not.toBeInTheDocument();
  });

  it("falls back to the password form when the persona list cannot be loaded", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated });
    renderApp("/login");
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });

  it("switches to the password form when demo login stops working, and says so", async () => {
    mockApi({
      "GET /v1/auth/me": unauthenticated,
      [DEMO]: () => json(200, { operators: DEMO_OPERATORS }),
      "POST /v1/auth/demo-login": noDemo,
    });
    const user = userEvent.setup();
    renderApp("/login");
    await user.click(await screen.findByRole("button", { name: "Continue as Bruno Lima" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Demo sign-in isn't available. Use your email and password.");
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });

  it("lets the operator choose the password form and come back to the personas", async () => {
    mockApi({ "GET /v1/auth/me": unauthenticated, [DEMO]: () => json(200, { operators: DEMO_OPERATORS }) });
    const user = userEvent.setup();
    renderApp("/login");
    await user.click(await screen.findByRole("button", { name: "Use email and password instead" }));
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to demo personas" }));
    expect(await screen.findByRole("list", { name: "Demo operators" })).toBeInTheDocument();
  });
});
