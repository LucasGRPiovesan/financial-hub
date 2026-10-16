import { expect, PERSONAS, test } from "../fixtures";
import { BillingPage } from "../pages/billing-page";
import { EventsPage } from "../pages/events-page";

test.describe("Events and billing", () => {
  test("an owner opens the event explorer and an event detail", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const events = new EventsPage(page);
    await events.goto();

    await expect(events.table).toBeVisible();
    await events.openFirst();

    // Envelope, deliveries and causality are the three things an event page must answer.
    await expect(page.getByRole("heading", { name: "Envelope" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Entregas" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Causalidade" })).toBeVisible();
  });

  test("an owner creates a billable metric and sees it in the table", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const billing = new BillingPage(page);
    await billing.goto();

    const code = `e2e_${Date.now().toString(36)}`;
    await billing.createMetric(code, "Chamadas de teste");

    const row = billing.metricsTable.getByRole("row", { name: new RegExp(code) });
    await expect(row).toContainText("Chamadas de teste");
    await expect(row).toContainText("Contagem");
    await expect(page.getByText("Planos, assinaturas e faturas")).toBeVisible();
  });
});
