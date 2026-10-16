import { expect, type Locator, type Page } from "@playwright/test";

import { PERSONAS, type Persona } from "../support/personas";

/** The sign-in screen with the demo operator picker. Role/label locators only. */
export class LoginPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto("/login");
    await expect(this.page.getByRole("heading", { name: /sign in/i })).toBeVisible();
  }

  /** The control that selects one operator (a button or a radio named after the operator). */
  persona(persona: Persona): Locator {
    const name = new RegExp(persona.displayName, "i");
    return this.page.getByRole("button", { name }).or(this.page.getByRole("radio", { name }));
  }

  /** True when this build of the console offers the demo picker (the screen may still be under construction). */
  async hasPicker(): Promise<boolean> {
    // The picker loads its operators from the API, so give it the same time as any web-first assertion.
    return this.persona(PERSONAS.ana)
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(
        () => true,
        () => false,
      );
  }

  /** Pick an operator: each persona card has its own "Continue as <name>" button, which signs in. */
  async continueAs(persona: Persona): Promise<void> {
    await this.persona(persona).click();
  }
}
