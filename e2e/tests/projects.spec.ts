import { expect, PERSONAS, test } from "../fixtures";
import { ProjectsPage } from "../pages/projects-page";
import { Shell } from "../pages/shell";
import { uniqueName, uniqueSlug } from "../support/data";
import { DEMO_PROJECT_NAME } from "../support/personas";

test.describe("projects", () => {
  test("an owner creates a project and switches between projects", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const projects = new ProjectsPage(page);
    const shell = new Shell(page);
    const name = uniqueName("project");

    await projects.goto();
    await projects.create(name, uniqueSlug());
    // A new project becomes the active one.
    await expect(shell.projectSwitcher(name)).toBeVisible();
    await expect(projects.row(name)).toBeVisible();

    await shell.switchProject(DEMO_PROJECT_NAME);
    await expect(shell.projectSwitcher(DEMO_PROJECT_NAME)).toBeVisible();

    await shell.switchProject(name);
    await expect(shell.projectSwitcher(name)).toBeVisible();
    // The choice survives a reload (kept per browser).
    await page.reload();
    await expect(shell.projectSwitcher(name)).toBeVisible();
  });
});
