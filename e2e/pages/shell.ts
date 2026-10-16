import { expect, type Locator, type Page } from "@playwright/test";

import type { Persona } from "../support/personas";

/** The signed-in frame: project switcher, theme menu, operator menu and sidebar navigation. */
export class Shell {
  constructor(private readonly page: Page) {}

  projectSwitcher(name?: string): Locator {
    return this.page.getByRole("button", { name: name ? `Projeto: ${name}` : /^Projeto:/ });
  }

  operatorMenu(persona: Persona): Locator {
    return this.page.getByRole("button", { name: `Menu do operador: ${persona.displayName}` });
  }

  themeMenu(): Locator {
    return this.page.getByRole("button", { name: /^Tema/ });
  }

  async expectSignedInAs(persona: Persona): Promise<void> {
    await expect(this.operatorMenu(persona)).toBeVisible();
  }

  async signOut(persona: Persona): Promise<void> {
    await this.operatorMenu(persona).click();
    await this.page.getByRole("menuitem", { name: "Sair" }).click();
  }

  async openSection(label: string): Promise<void> {
    await this.page.getByRole("link", { name: label, exact: true }).first().click();
  }

  async switchProject(name: string): Promise<void> {
    await this.projectSwitcher().click();
    await this.page.getByRole("menuitemradio", { name: new RegExp(name) }).click();
  }
}
