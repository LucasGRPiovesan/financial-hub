import { expect, PERSONAS, test } from "../fixtures";
import { LedgerPage } from "../pages/ledger-page";

test.describe("Ledger and audit", () => {
  test("an owner sees a balanced trial balance and opens a journal entry whose debits equal its credits", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const ledger = new LedgerPage(page);
    await ledger.goto();

    await expect(ledger.trialBalance).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("Balanceado", { exact: true })).toBeVisible();
    const debits = await ledger.trialBalance.getByTestId("trial-debits").innerText();
    const credits = await ledger.trialBalance.getByTestId("trial-credits").innerText();
    expect(debits).toBe(credits);

    await ledger.openFirstEntry();
    await expect(ledger.postings).toBeVisible({ timeout: 30_000 });
    await expect(ledger.postings.getByText("Débitos iguais a créditos")).toBeVisible();
    const entryDebits = await ledger.postings.getByTestId("total-debits").innerText();
    const entryCredits = await ledger.postings.getByTestId("total-credits").innerText();
    expect(entryDebits).toBe(entryCredits);
  });

  test("the journal names its recipes in Portuguese", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const ledger = new LedgerPage(page);
    await ledger.goto();
    await ledger.tab("Lançamentos").click();
    await expect(ledger.entries.getByText("Pagamento capturado").first()).toBeVisible({ timeout: 30_000 });
  });

  test("an owner reads the audit log with humanized actions", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    await page.goto("/");
    await page.getByRole("link", { name: "Auditoria", exact: true }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "Auditoria" })).toBeVisible({ timeout: 30_000 });
    const table = page.getByRole("table", { name: "Registros de auditoria" });
    await expect(table).toBeVisible({ timeout: 30_000 });
    await expect(table.getByText("customer.created").first()).toBeVisible();
    await expect(table.getByText(/Cliente criado|cliente .* foi criado/i).first()).toBeVisible();
  });

  test("a viewer is told why the audit log is closed to them", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.diego);
    await page.goto("/");
    await page.getByRole("link", { name: "Auditoria", exact: true }).first().click();
    await expect(page.getByText("Sem acesso ao registro de auditoria")).toBeVisible();
  });
});
