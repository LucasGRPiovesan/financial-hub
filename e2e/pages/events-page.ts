import { expect, type Locator, type Page } from "@playwright/test";

export class EventsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Client-side navigation: a hard load of a console route can be swallowed by the dev proxy (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Eventos", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Eventos" });
  }

  get table(): Locator {
    return this.page.getByRole("table", { name: "Eventos, do mais recente ao mais antigo" });
  }

  /** Opens the first listed event and waits for its detail page. */
  async openFirst(): Promise<void> {
    await this.table.getByRole("link", { name: /^Abrir evento / }).first().click();
    await expect(this.page.getByRole("heading", { level: 1, name: "Evento" })).toBeVisible();
  }
}
