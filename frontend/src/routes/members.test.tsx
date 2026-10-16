import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { member, overviewData, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const MEMBERS = "GET /v1/projects/p-1/members";
const ANA = member();
const BRUNO = member({ operator_id: "op-2", email: "bruno@example.test", display_name: "Bruno Lima", role: "admin" });
const CARLA = member({ operator_id: "op-3", email: "carla@example.test", display_name: "Carla Dias", role: "owner" });
const DAVI = member({ operator_id: "op-4", email: "davi@example.test", display_name: "Davi Rocha", role: "viewer" });

const meAs = (role: string) => ({
  ...ME,
  memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]],
});

function base(role: string, rows = [ANA, BRUNO, CARLA, DAVI]) {
  return {
    "GET /v1/auth/me": () => json(200, meAs(role)),
    ...overviewData("p-1"),
    [MEMBERS]: () => json(200, { data: rows }),
  };
}

describe("Members screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lets an owner change every role and remove any member, without axe violations", async () => {
    mockApi(base("owner"));
    const { container } = renderApp("/members");

    expect(await screen.findByRole("heading", { name: "Members" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Project members" });
    expect(within(table).getByText("(you)")).toBeInTheDocument();
    for (const name of ["Ana Souza", "Bruno Lima", "Carla Dias", "Davi Rocha"]) {
      expect(within(table).getByRole("combobox", { name: `Role for ${name}` })).toBeEnabled();
      expect(within(table).getByRole("button", { name: `Remove ${name}` })).toBeEnabled();
    }
    expect(screen.getByRole("button", { name: "Add member" })).toBeEnabled();
    const ownerOption = within(screen.getByRole("combobox", { name: "Role for Davi Rocha" })).getByRole("option", { name: "owner" });
    expect(ownerOption).toBeEnabled();
    await expectNoAxeViolations(container);
  });

  it("keeps everything owner-related out of an admin's reach", async () => {
    mockApi(base("admin"));
    renderApp("/members");

    const table = await screen.findByRole("table", { name: "Project members" });
    // Owners' rows are read-only for a non-owner...
    expect(within(table).queryByRole("combobox", { name: "Role for Carla Dias" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Remove Carla Dias" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("combobox", { name: "Role for Ana Souza" })).not.toBeInTheDocument();
    // ...other rows stay editable, but the owner role cannot be handed out.
    expect(within(table).getByRole("combobox", { name: "Role for Davi Rocha" })).toBeEnabled();
    expect(within(table).getByRole("button", { name: "Remove Davi Rocha" })).toBeEnabled();
    const ownerOption = within(screen.getByRole("combobox", { name: "Role for Davi Rocha" })).getByRole("option", { name: "owner" });
    expect(ownerOption).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add member" })).toBeEnabled();
  });

  it("is read-only for a viewer and says why", async () => {
    mockApi(base("viewer"));
    const { container } = renderApp("/members");

    const table = await screen.findByRole("table", { name: "Project members" });
    expect(within(table).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add member" })).toBeDisabled();
    expect(screen.getByText(/Your role \(viewer\) can't/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows a skeleton, then the problem message with a retry", async () => {
    let fail = true;
    mockApi({
      ...base("owner"),
      [MEMBERS]: () => (fail ? problem(500, "Database is down") : json(200, { data: [ANA] })),
    });
    const user = userEvent.setup();
    renderApp("/members");
    expect(await screen.findByText("Database is down")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load members")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Project members" })).toBeInTheDocument();
  });

  it("explains an empty project", async () => {
    mockApi(base("owner", []));
    renderApp("/members");
    expect(await screen.findByText("No members yet")).toBeInTheDocument();
  });

  it("adds a member by email and role, then shows the refreshed list", async () => {
    let rows = [ANA];
    const { calls } = mockApi({
      ...base("owner"),
      [MEMBERS]: () => json(200, { data: rows }),
      "POST /v1/projects/p-1/members": ({ init }) => {
        expect(JSON.parse(String(init.body))).toEqual({ email: "bruno@example.test", role: "finance" });
        rows = [ANA, { ...BRUNO, role: "finance" }];
        return json(201, { ...BRUNO, role: "finance" });
      },
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.click(await screen.findByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    await user.type(within(dialog).getByLabelText("Email"), "bruno@example.test");
    await user.selectOptions(within(dialog).getByLabelText("Role"), "finance");
    await user.click(within(dialog).getByRole("button", { name: "Add member" }));

    expect(await screen.findByRole("combobox", { name: "Role for Bruno Lima" })).toHaveValue("finance");
    expect(screen.queryByRole("dialog", { name: "Add member" })).not.toBeInTheDocument();
    expect(calls.filter((call) => call.key === "POST /v1/projects/p-1/members")).toHaveLength(1);
  });

  it("puts an unknown email on the email field", async () => {
    mockApi({
      ...base("owner"),
      "POST /v1/projects/p-1/members": () =>
        problem(422, "Invalid", [{ pointer: "/email", detail: "no active operator has this email" }]),
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.click(await screen.findByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    await user.type(within(dialog).getByLabelText("Email"), "ghost@example.test");
    await user.click(within(dialog).getByRole("button", { name: "Add member" }));

    expect(await within(dialog).findByText("no active operator has this email")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("explains a duplicate member (409) on the email field", async () => {
    mockApi({
      ...base("owner"),
      "POST /v1/projects/p-1/members": () => problem(409, "Conflict", [{ pointer: "/email", detail: "already a member" }]),
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.click(await screen.findByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add member" });
    await user.type(within(dialog).getByLabelText("Email"), "bruno@example.test");
    await user.click(within(dialog).getByRole("button", { name: "Add member" }));
    expect(await within(dialog).findByText("already a member")).toBeInTheDocument();
  });

  it("changes a role", async () => {
    const { calls } = mockApi({
      ...base("owner"),
      "PATCH /v1/projects/p-1/members/op-4": ({ init }) => {
        expect(JSON.parse(String(init.body))).toEqual({ role: "finance" });
        return json(200, { ...DAVI, role: "finance" });
      },
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Role for Davi Rocha" }), "finance");
    expect(await screen.findByText("Role changed")).toBeInTheDocument();
    expect(calls.some((call) => call.key === "PATCH /v1/projects/p-1/members/op-4")).toBe(true);
  });

  it("tells the operator the project must keep an owner (409)", async () => {
    mockApi({
      ...base("owner", [ANA]),
      "PATCH /v1/projects/p-1/members/op-1": () => problem(409, "A project must keep at least one owner."),
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Role for Ana Souza" }), "admin");
    expect(await screen.findByText("Couldn't change the role")).toBeInTheDocument();
    expect(screen.getByText("A project must keep at least one owner.")).toBeInTheDocument();
  });

  it("explains that only owners manage the owner role (403)", async () => {
    mockApi({
      ...base("admin"),
      "PATCH /v1/projects/p-1/members/op-4": () => problem(403, "Forbidden"),
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Role for Davi Rocha" }), "finance");
    expect(await screen.findByText("Only an owner can grant, change or remove the owner role.")).toBeInTheDocument();
  });

  it("asks for confirmation before removing a member", async () => {
    let rows = [ANA, DAVI];
    const { calls } = mockApi({
      ...base("owner"),
      [MEMBERS]: () => json(200, { data: rows }),
      "DELETE /v1/projects/p-1/members/op-4": () => {
        rows = [ANA];
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.click(await screen.findByRole("button", { name: "Remove Davi Rocha" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove member" });
    expect(calls.some((call) => call.key.startsWith("DELETE"))).toBe(false);
    expect(within(dialog).getByText(/will lose access to this project immediately/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Remove member" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove Davi Rocha" })).not.toBeInTheDocument());
    expect(await screen.findByText("Member removed")).toBeInTheDocument();
  });

  it("keeps the dialog open and explains a refused removal", async () => {
    mockApi({
      ...base("owner", [ANA]),
      "DELETE /v1/projects/p-1/members/op-1": () => problem(409, "A project must keep at least one owner."),
    });
    const user = userEvent.setup();
    renderApp("/members");
    await user.click(await screen.findByRole("button", { name: "Remove Ana Souza" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove member" });
    await user.click(within(dialog).getByRole("button", { name: "Remove member" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A project must keep at least one owner.");
  });

  it("shows an empty state when there is no project", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, { ...ME, memberships: [] }) });
    renderApp("/members");
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
  });
});
