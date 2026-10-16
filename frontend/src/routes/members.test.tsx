import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { overviewData } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const base = { "GET /v1/auth/me": () => json(200, ME), ...overviewData("p-1") };

describe("Members screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the operator's role and says member management is not available yet", async () => {
    mockApi(base);
    const { container } = renderApp("/members");
    expect(await screen.findByText("Member management isn't available yet")).toBeInTheDocument();
    expect(screen.getByText(/Your role can manage members/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("explains a read-only role", async () => {
    window.localStorage.setItem("fh.project", "p-2");
    mockApi(base);
    renderApp("/members");
    expect(await screen.findByText(/Your role \(viewer\) can't manage members/)).toBeInTheDocument();
  });

  it("offers Create project when the operator has none", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, { ...ME, memberships: [] }) });
    renderApp("/members");
    expect(await screen.findByRole("button", { name: "Create project" })).toBeInTheDocument();
  });
});
