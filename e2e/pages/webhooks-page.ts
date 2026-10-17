import { expect, type Locator, type Page } from "@playwright/test";

export class WebhooksPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Client-side navigation: a hard load of a console route can be swallowed by the dev proxy (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Webhooks", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Webhooks" });
  }

  get addButton(): Locator {
    return this.page.getByRole("button", { name: "Adicionar endpoint" });
  }

  /** The read-only field in the "shown once" panel. */
  get issuedSecret(): Locator {
    return this.page.getByRole("textbox", { name: "Segredo de assinatura" });
  }

  openLink(url: string): Locator {
    return this.page.getByRole("link", { name: `Abrir endpoint ${url}` });
  }

  /** Create an endpoint for all events and leave the "shown once" panel open (read `issuedSecret` while it is up). */
  async create(url: string, description: string): Promise<void> {
    await this.addButton.click();
    const dialog = this.page.getByRole("dialog", { name: "Adicionar endpoint de webhook" });
    await dialog.getByLabel("URL do endpoint").fill(url);
    await dialog.getByLabel("Descrição").fill(description);
    await dialog.getByRole("button", { name: "Criar endpoint" }).click();
    await expect(this.issuedSecret).toBeVisible();
  }

  async confirmSaved(): Promise<void> {
    await this.page.getByRole("button", { name: "Já guardei" }).click();
    await expect(this.issuedSecret).toHaveCount(0);
  }
}
