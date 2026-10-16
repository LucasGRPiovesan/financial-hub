import { expect, type Locator, type Page } from "@playwright/test";

export class PaymentsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Client-side navigation: a hard load of a console route can be swallowed by the dev proxy (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Pagamentos", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Pagamentos" });
  }

  get table(): Locator {
    return this.page.getByRole("table", { name: "Pagamentos" });
  }

  /** A listed payment, found by the external reference the seed gives it (demo-pay-02). */
  row(externalRef: string): Locator {
    return this.table.getByRole("row", { name: new RegExp(externalRef) });
  }

  async open(externalRef: string): Promise<void> {
    await this.row(externalRef).getByRole("link", { name: /^Abrir cobrança / }).click();
    await expect(this.page.getByText("Valor da cobrança")).toBeVisible();
  }

  get refundButton(): Locator {
    return this.page.getByRole("button", { name: "Estornar cobrança" });
  }

  async refundPartially(amount: string, reason: string): Promise<void> {
    await this.refundButton.click();
    const dialog = this.page.getByRole("dialog", { name: "Estornar cobrança" });
    await dialog.getByLabel("Estorno parcial").check();
    await dialog.getByLabel("Valor a estornar (R$)").fill(amount);
    await dialog.getByLabel("Motivo (opcional)").fill(reason);
    await dialog.getByRole("button", { name: "Confirmar estorno" }).click();
    await expect(dialog).toHaveCount(0);
  }
}
