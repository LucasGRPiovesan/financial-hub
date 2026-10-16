import { expect, type Page } from "@playwright/test";

export class OverviewPage {
  constructor(private readonly page: Page) {}

  async expectLoaded(): Promise<void> {
    await expect(this.page).toHaveURL(/\/$/);
    await expect(this.page.getByRole("heading", { level: 1 })).toBeVisible();
  }
}
