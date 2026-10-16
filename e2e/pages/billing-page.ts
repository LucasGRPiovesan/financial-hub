import { expect, type Locator, type Page } from "@playwright/test";

export class BillingPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Cobrança", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Cobrança" });
  }

  get metricsTable(): Locator {
    return this.page.getByRole("table", { name: "Métricas faturáveis" });
  }

  async createMetric(code: string, name: string): Promise<void> {
    await this.page.getByRole("button", { name: "Nova métrica" }).first().click();
    const dialog = this.page.getByRole("dialog", { name: "Nova métrica faturável" });
    await dialog.getByLabel("Código").fill(code);
    await dialog.getByLabel("Nome", { exact: true }).fill(name);
    await dialog.getByLabel("Agregação").selectOption("count");
    await dialog.getByRole("button", { name: "Criar métrica" }).click();
    await expect(dialog).toHaveCount(0);
  }
}
