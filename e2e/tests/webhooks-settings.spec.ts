import { expect, PERSONAS, test } from "../fixtures";
import { WebhooksPage } from "../pages/webhooks-page";
import { uniqueName } from "../support/data";

test.describe("Webhooks and settings", () => {
  test("an owner creates an endpoint, sees the secret once and sends a test event", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const webhooks = new WebhooksPage(page);
    await webhooks.goto();
    const url = "https://example.com/hooks";
    const description = uniqueName("hook");

    await webhooks.create(url, description);
    await expect(webhooks.issuedSecret).toHaveValue(/^whsec_/);
    await webhooks.confirmSaved();
    await expect(page.getByRole("row", { name: new RegExp(description) })).toBeVisible();

    await page.getByRole("row", { name: new RegExp(description) }).getByRole("link", { name: `Abrir endpoint ${url}` }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Endpoint de webhook" })).toBeVisible();
    await expect(webhooks.issuedSecret).toHaveCount(0);

    await page.getByRole("button", { name: "Enviar evento de teste" }).click();
    await expect(page.getByRole("link", { name: "Ver evento" })).toBeVisible();

    // The e2e stack runs no worker (deploy/compose.e2e.yaml: db, migrate, api, web), so the delivery stays pending.
    await page.getByRole("link", { name: "Ver evento" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Evento" })).toBeVisible();
    const deliveries = page.getByRole("table", { name: "Entregas deste evento" });
    await expect(deliveries.getByText("Pendente")).toBeVisible();
  });

  test("a viewer is told why webhooks are unavailable", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.diego);
    await page.goto("/");
    await page.getByRole("link", { name: "Webhooks", exact: true }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "Webhooks" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Adicionar endpoint" })).toBeDisabled();
    await expect(page.getByRole("table", { name: "Endpoints de webhook" })).toHaveCount(0);
  });

  test("settings shows the operator, the project and the sandbox environment", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    await page.goto("/");
    await page.getByRole("link", { name: "Configurações", exact: true }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "Configurações" })).toBeVisible();
    await expect(page.getByText("Sandbox", { exact: true })).toBeVisible();
    await expect(page.getByRole("radiogroup", { name: "Idioma" })).toBeVisible();
    await expect(page.getByRole("link", { name: "README" })).toBeVisible();
  });
});
