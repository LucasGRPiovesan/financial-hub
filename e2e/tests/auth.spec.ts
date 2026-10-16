import { expect, PERSONAS, test } from "../fixtures";
import { LoginPage } from "../pages/login-page";
import { OverviewPage } from "../pages/overview-page";
import { Shell } from "../pages/shell";
import { DEMO_PROJECT_NAME, PERSONA_LIST } from "../support/personas";

const { ana } = PERSONAS;

test.describe("sign in with the demo picker (UI)", () => {
  test("the picker lists the four demo operators", async ({ page }) => {
    const login = new LoginPage(page);
    await login.goto();
    test.fixme(!(await login.hasPicker()), "TODO(login screen): the demo operator picker is not in this build yet");

    for (const persona of PERSONA_LIST) {
      await expect(login.persona(persona)).toBeVisible();
    }
  });

  test("continue as Ana lands on the overview with Demo Store", async ({ page }) => {
    const login = new LoginPage(page);
    await login.goto();
    test.fixme(!(await login.hasPicker()), "TODO(login screen): the demo operator picker is not in this build yet");

    await login.continueAs(ana);

    const shell = new Shell(page);
    await shell.expectSignedInAs(ana);
    await new OverviewPage(page).expectLoaded();
    await expect(shell.projectSwitcher(DEMO_PROJECT_NAME)).toBeVisible();
  });
});

test.describe("session", () => {
  test("a refresh keeps the session", async ({ loginAs }) => {
    const page = await loginAs(ana);
    await page.goto("/");
    const shell = new Shell(page);
    await shell.expectSignedInAs(ana);

    await page.reload();

    await shell.expectSignedInAs(ana);
    await expect(shell.projectSwitcher(DEMO_PROJECT_NAME)).toBeVisible();
  });

  test("signing out returns to the sign-in screen", async ({ loginAs }) => {
    const page = await loginAs(ana);
    await page.goto("/");
    const shell = new Shell(page);
    await shell.expectSignedInAs(ana);

    await shell.signOut(ana);

    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: /entrar/i })).toBeVisible();
    // The session is really gone: protected routes bounce back to sign in.
    await page.goto("/projects");
    await expect(page).toHaveURL(/\/login/);
  });
});
