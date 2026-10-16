import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { customer, overviewData, page, problem } from "../test/console-mocks";
import { json, ME, mockApi, renderApp, resetApiState } from "../test/harness";

const LIST = "GET /v1/projects/p-1/customers?limit=25";
const MARINA = customer();
const JOAO = customer({
  id: "c-2",
  external_ref: "cust_2001",
  name: "João Pereira",
  email: "joao@example.test",
  document_masked: null,
  metadata: {},
});
// Both documents pass the check digits.
const VALID_CPF = "529.982.247-25";
const VALID_CNPJ = "11.222.333/0001-81";

const meAs = (role: string) => ({
  ...ME,
  memberships: [{ project_id: "p-1", project_name: "Acme Payments", role }, ME.memberships[1]],
});

function base(role = "owner", rows: unknown[] = [MARINA, JOAO]) {
  return {
    "GET /v1/auth/me": () => json(200, meAs(role)),
    ...overviewData("p-1"),
    [LIST]: () => json(200, page(rows)),
  };
}

describe("Customers screen", () => {
  beforeEach(() => resetApiState());
  afterEach(() => vi.unstubAllGlobals());

  it("lists customers with the masked document only, without axe violations", async () => {
    mockApi(base());
    const { container } = renderApp("/customers");

    expect(await screen.findByRole("heading", { name: "Customers" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Customers" });
    expect(within(table).getByRole("button", { name: "Open cust_1042" })).toBeInTheDocument();
    expect(within(table).getByText("Marina Alves")).toBeInTheDocument();
    expect(within(table).getByText("***.456.789-**")).toBeInTheDocument();
    expect(screen.getByText("2 customers loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows a skeleton, then the problem message with a retry", async () => {
    let fail = true;
    mockApi({ ...base(), [LIST]: () => (fail ? problem(500, "Database is down") : json(200, page([MARINA]))) });
    const user = userEvent.setup();
    renderApp("/customers");
    expect(await screen.findByText("Database is down")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load customers")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Customers" })).toBeInTheDocument();
  });

  it("invites the operator to create the first customer", async () => {
    mockApi(base("owner", []));
    renderApp("/customers");
    expect(await screen.findByText("No customers yet")).toBeInTheDocument();
  });

  it("loads the next page with the cursor", async () => {
    const { calls } = mockApi({
      ...base(),
      [LIST]: () => json(200, { data: [MARINA], has_more: true, next_cursor: "cur-1" }),
      "GET /v1/projects/p-1/customers?limit=25&cursor=cur-1": () => json(200, page([JOAO])),
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await screen.findByRole("table", { name: "Customers" });
    expect(screen.getByText("1 customer loaded, more available")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("button", { name: "Open cust_2001" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open cust_1042" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(calls.some((call) => call.key.endsWith("&cursor=cur-1"))).toBe(true);
  });

  it("filters by external reference or email, and clears the filter", async () => {
    const { calls } = mockApi({
      ...base(),
      "GET /v1/projects/p-1/customers?limit=25&external_ref=cust_2001": () => json(200, page([JOAO])),
      "GET /v1/projects/p-1/customers?limit=25&email=nobody%40example.test": () => json(200, page([])),
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await screen.findByRole("table", { name: "Customers" });

    const search = screen.getByRole("search", { name: "Filter customers" });
    await user.type(within(search).getByLabelText("Exact value"), "cust_2001");
    await user.click(within(search).getByRole("button", { name: "Apply filter" }));
    expect(await screen.findByRole("button", { name: "Open cust_2001" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open cust_1042" })).not.toBeInTheDocument();

    await user.selectOptions(within(search).getByLabelText("Filter by"), "email");
    await user.clear(within(search).getByLabelText("Exact value"));
    await user.type(within(search).getByLabelText("Exact value"), "nobody@example.test");
    await user.click(within(search).getByRole("button", { name: "Apply filter" }));
    expect(await screen.findByText("No customer matches")).toBeInTheDocument();

    await user.click(within(search).getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByRole("button", { name: "Open cust_1042" })).toBeInTheDocument();
    expect(calls.some((call) => call.key.includes("external_ref=cust_2001"))).toBe(true);
  });

  it("opens the detail drawer with identity fields, masked document and metadata", async () => {
    mockApi(base());
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "Open cust_1042" }));

    const drawer = await screen.findByRole("dialog", { name: "cust_1042" });
    expect(within(drawer).getByText("***.456.789-**")).toBeInTheDocument();
    expect(within(drawer).getByText("marina@example.test")).toBeInTheDocument();
    expect(within(drawer).getByRole("heading", { name: "Metadata" })).toBeInTheDocument();
    expect(within(drawer).getByText("plan")).toBeInTheDocument();
    expect(within(drawer).getByText("pro")).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Edit customer" })).toBeEnabled();
    expect(within(drawer).getByRole("button", { name: "Anonymize" })).toBeEnabled();
  });

  it("validates the CPF before sending anything", async () => {
    const { calls } = mockApi(base("owner", []));
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "New customer" }));
    const dialog = await screen.findByRole("dialog", { name: "New customer" });

    await user.type(within(dialog).getByLabelText("External reference"), "cust_9");
    await user.type(within(dialog).getByLabelText("Name"), "Rafaela Costa");
    await user.type(within(dialog).getByLabelText("CPF"), "111.111.111-11");
    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));

    expect(await within(dialog).findByText("This CPF's check digits are wrong.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("CPF")).toHaveAttribute("aria-invalid", "true");
    expect(calls.some((call) => call.key.startsWith("PUT"))).toBe(false);
  });

  it("validates the CNPJ for a company and requires a name and a reference", async () => {
    const { calls } = mockApi(base("owner", []));
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "New customer" }));
    const dialog = await screen.findByRole("dialog", { name: "New customer" });

    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));
    expect(await within(dialog).findByText("Enter the reference your system uses for this customer.")).toBeInTheDocument();
    expect(within(dialog).getByText("Enter the customer's name.")).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Type"), "company");
    await user.type(within(dialog).getByLabelText("CNPJ"), "11.222.333/0001-82");
    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));
    expect(await within(dialog).findByText("This CNPJ's check digits are wrong.")).toBeInTheDocument();
    expect(calls.some((call) => call.key.startsWith("PUT"))).toBe(false);
  });

  it("creates a customer with PUT by external reference and never shows the document again", async () => {
    let rows = [MARINA];
    let sentBody: unknown = null;
    mockApi({
      ...base(),
      [LIST]: () => json(200, page(rows)),
      "PUT /v1/projects/p-1/customers/cust_9": ({ init }) => {
        sentBody = JSON.parse(String(init.body));
        const saved = customer({
          id: "c-9",
          external_ref: "cust_9",
          name: "Rafaela Costa",
          email: "rafaela@example.test",
          document_masked: "***.982.247-**",
          metadata: { plan: "free" },
        });
        rows = [MARINA, saved];
        return json(200, saved);
      },
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "New customer" }));
    const dialog = await screen.findByRole("dialog", { name: "New customer" });
    await user.type(within(dialog).getByLabelText("External reference"), "cust_9");
    await user.type(within(dialog).getByLabelText("Name"), "Rafaela Costa");
    await user.type(within(dialog).getByLabelText("CPF"), VALID_CPF);
    await user.type(within(dialog).getByLabelText("Email"), "rafaela@example.test");
    await user.click(within(dialog).getByLabelText("Metadata"));
    await user.paste('{"plan": "free"}');
    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));

    // The saved customer opens in the drawer, showing only the masked document.
    const drawer = await screen.findByRole("dialog", { name: "cust_9" });
    expect(within(drawer).getByText("***.982.247-**")).toBeInTheDocument();
    expect(sentBody).toEqual({
      name: "Rafaela Costa",
      kind: "individual",
      email: "rafaela@example.test",
      phone: null,
      metadata: { plan: "free" },
      document: VALID_CPF,
    });
    expect(document.body.textContent).not.toContain("529.982.247-25");
    expect(document.body.textContent).not.toContain("52998224725");
    // The list behind the modal drawer refetched and includes it (hidden from the a11y tree while the drawer is open).
    expect(await screen.findByRole("button", { name: "Open cust_9", hidden: true })).toBeInTheDocument();
  });

  it("maps server validation errors onto the fields", async () => {
    mockApi({
      ...base("owner", []),
      "PUT /v1/projects/p-1/customers/cust_9": () =>
        problem(422, "Invalid", [
          { pointer: "/document", detail: "document is not valid for this kind" },
          { pointer: "/email", detail: "email is not valid" },
        ]),
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "New customer" }));
    const dialog = await screen.findByRole("dialog", { name: "New customer" });
    await user.type(within(dialog).getByLabelText("External reference"), "cust_9");
    await user.type(within(dialog).getByLabelText("Name"), "Rafaela Costa");
    await user.type(within(dialog).getByLabelText("CPF"), VALID_CPF);
    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));

    expect(await within(dialog).findByText("document is not valid for this kind")).toBeInTheDocument();
    expect(within(dialog).getByText("email is not valid")).toBeInTheDocument();
    // The document field was emptied after the submit, whatever the outcome.
    expect(within(dialog).getByLabelText("CPF")).toHaveValue("");
  });

  it("explains a conflict (409) in the dialog and a company with a valid CNPJ goes through", async () => {
    let attempts = 0;
    mockApi({
      ...base("owner", []),
      "PUT /v1/projects/p-1/customers/cust_9": () => {
        attempts += 1;
        return attempts === 1
          ? problem(409, "This reference already belongs to a person; the type can't change once a document is on file.")
          : json(200, customer({ id: "c-9", external_ref: "cust_9", kind: "company", name: "Costa Ltda" }));
      },
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "New customer" }));
    const dialog = await screen.findByRole("dialog", { name: "New customer" });
    await user.type(within(dialog).getByLabelText("External reference"), "cust_9");
    await user.type(within(dialog).getByLabelText("Name"), "Costa Ltda");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "company");
    await user.type(within(dialog).getByLabelText("CNPJ"), VALID_CNPJ);
    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("the type can't change once a document is on file");

    await user.click(within(dialog).getByRole("button", { name: "Save customer" }));
    expect(await screen.findByRole("dialog", { name: "cust_9" })).toBeInTheDocument();
  });

  it("edits an existing customer without asking for the document again", async () => {
    let sentBody: Record<string, unknown> = {};
    mockApi({
      ...base(),
      "PUT /v1/projects/p-1/customers/cust_1042": ({ init }) => {
        sentBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return json(200, { ...MARINA, name: "Marina Alves Lima" });
      },
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "Open cust_1042" }));
    await user.click(await screen.findByRole("button", { name: "Edit customer" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit customer" });
    expect(within(dialog).getByLabelText("External reference")).toBeDisabled();
    expect(within(dialog).getByLabelText("CPF")).toHaveValue("");
    expect(within(dialog).getByText(/On file: \*\*\*\.456\.789-\*\*/)).toBeInTheDocument();

    const name = within(dialog).getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Marina Alves Lima");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(sentBody.name).toBe("Marina Alves Lima"));
    expect(sentBody).not.toHaveProperty("document");
  });

  it("makes the operator type the external reference before anonymizing", async () => {
    const calls: string[] = [];
    mockApi({
      ...base(),
      "POST /v1/projects/p-1/customers/c-1/anonymize": () => {
        calls.push("anonymize");
        return json(200, customer({ name: "", email: null, document_masked: null, metadata: {}, anonymized: true }));
      },
    });
    const user = userEvent.setup();
    renderApp("/customers");
    await user.click(await screen.findByRole("button", { name: "Open cust_1042" }));
    await user.click(await screen.findByRole("button", { name: "Anonymize" }));

    const dialog = await screen.findByRole("dialog", { name: "Anonymize customer" });
    const confirm = within(dialog).getByRole("button", { name: "Anonymize customer" });
    expect(confirm).toBeDisabled();
    const input = within(dialog).getByLabelText("Type cust_1042 to confirm");
    await user.type(input, "cust_104");
    expect(confirm).toBeDisabled();
    await user.type(input, "2");
    expect(confirm).toBeEnabled();
    expect(calls).toEqual([]);

    await user.click(confirm);
    expect(await screen.findByText("Customer anonymized")).toBeInTheDocument();
    expect(calls).toEqual(["anonymize"]);
  });

  it("is read-only for a viewer and says why", async () => {
    mockApi(base("viewer"));
    const user = userEvent.setup();
    const { container } = renderApp("/customers");

    expect(await screen.findByRole("button", { name: "New customer" })).toBeDisabled();
    await user.click(await screen.findByRole("button", { name: "Open cust_1042" }));
    const drawer = await screen.findByRole("dialog", { name: "cust_1042" });
    expect(within(drawer).getByRole("button", { name: "Edit customer" })).toBeDisabled();
    expect(within(drawer).getByRole("button", { name: "Anonymize" })).toBeDisabled();
    await expectNoAxeViolations(container);
  });

  it("shows an empty state when there is no project", async () => {
    mockApi({ "GET /v1/auth/me": () => json(200, { ...ME, memberships: [] }) });
    renderApp("/customers");
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
  });
});
