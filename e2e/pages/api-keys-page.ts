import { expect, type Locator, type Page } from "@playwright/test";

export class ApiKeysPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Navigate client-side: a hard load of /api-keys is swallowed by the dev server proxy for /api (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Chaves de API", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Chaves de API" });
  }

  get createButton(): Locator {
    return this.page.getByRole("button", { name: "Criar chave de API" });
  }

  revokeButton(keyName: string): Locator {
    return this.page.getByRole("button", { name: `Revogar ${keyName}` });
  }

  get anyRevokeButton(): Locator {
    return this.page.getByRole("button", { name: /^Revogar / });
  }

  /** The read-only field in the "shown once" panel. */
  get issuedKey(): Locator {
    return this.page.getByRole("textbox", { name: "Chave de API" });
  }

  row(keyName: string): Locator {
    return this.page.getByRole("row", { name: new RegExp(keyName) });
  }

  /** Create a key and leave the "shown once" panel open (read `issuedKey` while it is up). */
  async create(name: string, scope?: string | RegExp): Promise<void> {
    await this.createButton.click();
    const dialog = this.page.getByRole("dialog", { name: "Criar chave de API" });
    await dialog.getByLabel("Nome").fill(name);
    if (scope) await dialog.getByRole("checkbox", { name: scope }).check();
    // Submit from the keyboard: at 1280x720 the dialog is taller than the viewport and "Criar chave" cannot be
    // scrolled into view (reported in the PRO-273 findings); Enter in the name field is the same submit.
    await dialog.getByLabel("Nome").press("Enter");
    await expect(this.issuedKey).toBeVisible();
  }

  async confirmStored(): Promise<void> {
    await this.page.getByRole("button", { name: "Já guardei" }).click();
    await expect(this.issuedKey).toHaveCount(0);
  }

  async revoke(keyName: string): Promise<void> {
    await this.revokeButton(keyName).click();
    await this.page.getByRole("dialog", { name: "Revogar a chave de API?" }).getByRole("button", { name: "Revogar chave" }).click();
  }
}
