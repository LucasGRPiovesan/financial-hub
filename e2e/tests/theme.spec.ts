import { expect, PERSONAS, test } from "../fixtures";
import { Shell } from "../pages/shell";

test.describe("theme", () => {
  test("the chosen theme persists across a reload", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    await page.goto("/");
    const shell = new Shell(page);
    await shell.expectSignedInAs(PERSONAS.ana);
    test.fixme((await shell.themeMenu().count()) === 0, "TODO(top bar): no theme control in this build yet");

    await shell.themeMenu().click();
    await page.getByRole("menuitemradio", { name: "Escuro" }).click();
    await expect(shell.themeMenu()).toHaveAccessibleName("Tema: Escuro");

    await page.reload();
    await expect(shell.themeMenu()).toHaveAccessibleName("Tema: Escuro");
  });
});
