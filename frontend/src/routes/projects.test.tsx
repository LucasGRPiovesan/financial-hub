import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { LIST, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const PROJECTS = `GET /v1/projects${LIST}`;
const project = (id: string, name: string, slug: string) => ({
  id,
  name,
  slug,
  status: "active",
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
});
const base = { "GET /v1/auth/me": () => json(200, ME), ...overviewData("p-1") };
const two = [project("p-1", "Acme Payments", "acme"), project("p-2", "Beta Billing", "beta")];

describe("Projects screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists projects with the operator's role and gates settings by role", async () => {
    mockApi({ ...base, [PROJECTS]: () => json(200, page(two)) });
    const { container } = renderApp("/projects");
    const acme = (await screen.findByRole("cell", { name: /Acme Payments/ })).closest("tr") as HTMLElement;
    expect(within(acme).getByText("owner")).toBeInTheDocument();
    expect(within(acme).getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Settings for Acme Payments" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Settings for Beta Billing" })).toBeDisabled();
    await expectNoAxeViolations(container);
  });

  it("shows an empty state with Create project", async () => {
    mockApi({ ...base, [PROJECTS]: () => json(200, page([])) });
    renderApp("/projects");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Create project" }).length).toBeGreaterThan(0);
  });

  it("shows the problem message and retries", async () => {
    let fail = true;
    mockApi({ ...base, [PROJECTS]: () => (fail ? problem(500, "Boom") : json(200, page(two))) });
    const user = userEvent.setup();
    renderApp("/projects");
    expect(await screen.findByText("Boom")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("cell", { name: /Acme Payments/ })).toBeInTheDocument();
  });

  it("puts create errors on the slug field", async () => {
    mockApi({
      ...base,
      [PROJECTS]: () => json(200, page(two)),
      "POST /v1/projects": () => problem(409, "Conflict", [{ pointer: "/slug", detail: "Slug already taken" }]),
    });
    const user = userEvent.setup();
    renderApp("/projects");
    const open = (await screen.findAllByRole("button", { name: "Create project" }))[0] as HTMLElement;
    await user.click(open);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "New");
    await user.type(within(dialog).getByRole("textbox", { name: /Slug/ }), "acme");
    await user.click(within(dialog).getByRole("button", { name: "Create project" }));
    expect(await within(dialog).findByText("Slug already taken")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: /Slug/ })).toHaveAttribute("aria-invalid", "true");
  });

  it("renames a project the role may manage", async () => {
    const { calls } = mockApi({
      ...base,
      [PROJECTS]: () => json(200, page(two)),
      "PATCH /v1/projects/p-1": () => json(200, { ...two[0], name: "Acme 2" }),
    });
    const user = userEvent.setup();
    renderApp("/projects");
    await user.click(await screen.findByRole("button", { name: "Settings for Acme Payments" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("textbox", { name: "Name" });
    await user.clear(input);
    await user.type(input, "Acme 2");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Project renamed")).toBeInTheDocument();
    expect(calls.some((call) => call.key === "PATCH /v1/projects/p-1")).toBe(true);
  });
});
