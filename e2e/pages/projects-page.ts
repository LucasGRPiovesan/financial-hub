import { expect, type Locator, type Page } from "@playwright/test";

export class ProjectsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto("/projects");
    await expect(this.page.getByRole("heading", { level: 1, name: "Projetos" })).toBeVisible();
  }

  row(projectName: string): Locator {
    return this.page.getByRole("row", { name: new RegExp(projectName) });
  }

  async create(name: string, slug: string): Promise<void> {
    await this.page.getByRole("button", { name: "Criar projeto" }).first().click();
    const dialog = this.page.getByRole("dialog", { name: "Criar projeto" });
    await dialog.getByLabel("Nome").fill(name);
    await dialog.getByLabel("Slug").fill(slug);
    await dialog.getByRole("button", { name: "Criar projeto" }).click();
    await expect(dialog).toBeHidden();
  }
}
