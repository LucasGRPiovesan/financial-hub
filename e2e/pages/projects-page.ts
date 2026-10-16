import { expect, type Locator, type Page } from "@playwright/test";

export class ProjectsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto("/projects");
    await expect(this.page.getByRole("heading", { level: 1, name: "Projects" })).toBeVisible();
  }

  row(projectName: string): Locator {
    return this.page.getByRole("row", { name: new RegExp(projectName) });
  }

  async create(name: string, slug: string): Promise<void> {
    await this.page.getByRole("button", { name: "Create project" }).first().click();
    const dialog = this.page.getByRole("dialog", { name: "Create project" });
    await dialog.getByLabel("Name").fill(name);
    await dialog.getByLabel("Slug").fill(slug);
    await dialog.getByRole("button", { name: "Create project" }).click();
    await expect(dialog).toBeHidden();
  }
}
