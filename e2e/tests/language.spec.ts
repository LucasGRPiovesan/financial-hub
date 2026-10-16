import { expect, PERSONAS, test } from "../fixtures";
import { Shell } from "../pages/shell";

test.describe("language", () => {
  test("the console starts in Portuguese, switches to English and back, and remembers the choice", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    await page.goto("/");
    const shell = new Shell(page);
    await shell.expectSignedInAs(PERSONAS.ana);
    await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
    await expect(page.getByRole("heading", { level: 1, name: "Visão geral" })).toBeVisible();

    // Instant switch from the operator menu: no reload, everything re-renders (including the menu's own labels).
    await shell.operatorMenu(PERSONAS.ana).click();
    await page.getByRole("menuitemradio", { name: "English" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en-US");
    await expect(page.getByRole("button", { name: `Operator menu: ${PERSONAS.ana.displayName}` })).toBeVisible();
    await expect(page.getByRole("link", { name: "API keys", exact: true }).first()).toBeVisible();

    // The choice is kept per browser.
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();

    await page.getByRole("button", { name: `Operator menu: ${PERSONAS.ana.displayName}` }).click();
    await page.getByRole("menuitemradio", { name: "Português" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Visão geral" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
  });

  test("the sign-in screen has its own language control", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: /entrar/i })).toBeVisible();

    await page.getByRole("button", { name: "Idioma: Português" }).click();
    await page.getByRole("menuitemradio", { name: "English" }).click();
    await expect(page.getByRole("heading", { name: /sign in/i })).toBeVisible();
    await expect(page.getByRole("button", { name: "Language: English" })).toBeVisible();

    await page.getByRole("button", { name: "Language: English" }).click();
    await page.getByRole("menuitemradio", { name: "Português" }).click();
    await expect(page.getByRole("heading", { name: /entrar/i })).toBeVisible();
  });
});
