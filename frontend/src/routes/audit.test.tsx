import { configure, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../lib/locale";
import { expectNoAxeViolations } from "../test/axe";
import { auditRecord, overviewData, page } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const LIST = "GET /v1/projects/p-1/audit-records?limit=25";

const meAs = (role: string) => ({
  ...ME,
  memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]],
});

const RECORDS = [
  auditRecord({ id: "au-1", action: "api_key.created", diff: { after: { name: "Checkout key" } } }),
  auditRecord({
    id: "au-2",
    action: "customer.created",
    target_type: "customer",
    target_id: "c-1",
    diff: { after: { name: "[redacted]", email: "[redacted]", external_ref: "cust_1" } },
    request_id: "req-77",
  }),
];

function base(role = "owner") {
  return {
    "GET /v1/auth/me": () => json(200, meAs(role)),
    ...overviewData("p-1"),
    [LIST]: () => json(200, page(RECORDS)),
  };
}

describe("Audit", () => {
  // Lazy route chunks load on first render; a busy CI host needs more than the 1 s default.
  beforeAll(() => {
    configure({ asyncUtilTimeout: 10_000 });
  });
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists records with humanized actions, actor and target, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/audit");

    expect(await screen.findByRole("heading", { name: "Audit" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Audit records" });
    expect(within(table).getByText(/Checkout key/)).toBeInTheDocument();
    expect(within(table).getByText("api_key.created")).toBeInTheDocument();
    expect(within(table).getByText("k-1")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("humanizes actions in Portuguese", async () => {
    mockApi(base());
    setLocale("pt-BR");
    renderApp("/audit");

    const table = await screen.findByRole("table", { name: "Registros de auditoria" });
    expect(within(table).getByText(/Chave de API/i)).toBeInTheDocument();
  });

  it("expands a record to its metadata and never shows the redaction marker", async () => {
    mockApi(base());
    renderApp("/audit");

    await screen.findByRole("table", { name: "Audit records" });
    const toggles = await screen.findAllByRole("button", { name: /^Show details for/ });
    await userEvent.click(toggles[1]!);

    expect(await screen.findByText("req-77")).toBeInTheDocument();
    expect(screen.getByText(/cust_1/)).toBeInTheDocument();
    expect(screen.getByText(/"email": "hidden"/)).toBeInTheDocument();
    expect(screen.queryByText(/\[redacted\]/)).not.toBeInTheDocument();
  });

  it("filters the loaded records by text", async () => {
    mockApi(base());
    renderApp("/audit");

    const table = await screen.findByRole("table", { name: "Audit records" });
    await userEvent.type(screen.getByLabelText("Search records"), "customer");
    expect(within(table).queryByText("api_key.created")).not.toBeInTheDocument();
    expect(within(table).getByText("customer.created")).toBeInTheDocument();
  });

  it("tells a viewer why the audit log is closed to them", async () => {
    mockApi(base("viewer"));
    renderApp("/audit");

    expect(await screen.findByText("No access to the audit log")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Audit records" })).not.toBeInTheDocument();
  });
});
