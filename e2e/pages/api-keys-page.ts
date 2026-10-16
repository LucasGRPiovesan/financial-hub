import { expect, type Locator, type Page } from "@playwright/test";

export class ApiKeysPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Navigate client-side: a hard load of /api-keys is swallowed by the dev server proxy for /api (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "API keys", exact: true }).first().click();
    await expect(this.heading).toBeVisible();
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "API keys" });
  }

  get createButton(): Locator {
    return this.page.getByRole("button", { name: "Create API key" });
  }

  revokeButton(keyName: string): Locator {
    return this.page.getByRole("button", { name: `Revoke ${keyName}` });
  }

  get anyRevokeButton(): Locator {
    return this.page.getByRole("button", { name: /^Revoke / });
  }

  /** The read-only field in the "shown once" panel. */
  get issuedKey(): Locator {
    return this.page.getByRole("textbox", { name: "API key" });
  }

  row(keyName: string): Locator {
    return this.page.getByRole("row", { name: new RegExp(keyName) });
  }

  /** Create a key and leave the "shown once" panel open (read `issuedKey` while it is up). */
  async create(name: string, scope?: string | RegExp): Promise<void> {
    await this.createButton.click();
    const dialog = this.page.getByRole("dialog", { name: "Create API key" });
    await dialog.getByLabel("Name").fill(name);
    if (scope) await dialog.getByRole("checkbox", { name: scope }).check();
    // Submit from the keyboard: at 1280x720 the dialog is taller than the viewport and "Create key" cannot be
    // scrolled into view (reported in the PRO-273 findings); Enter in the name field is the same submit.
    await dialog.getByLabel("Name").press("Enter");
    await expect(this.issuedKey).toBeVisible();
  }

  async confirmStored(): Promise<void> {
    await this.page.getByRole("button", { name: "I stored it" }).click();
    await expect(this.issuedKey).toHaveCount(0);
  }

  async revoke(keyName: string): Promise<void> {
    await this.revokeButton(keyName).click();
    await this.page.getByRole("dialog", { name: "Revoke API key?" }).getByRole("button", { name: "Revoke key" }).click();
  }
}
