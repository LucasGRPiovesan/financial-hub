import { expect, PERSONAS, test } from "../fixtures";

const ROUTES = [
  "/",
  "/projects",
  "/customers",
  "/integrations",
  "/api-keys",
  "/members",
  "/ledger",
  "/payments",
  "/billing",
  "/events",
  "/webhooks",
  "/reconciliation",
  "/audit",
  "/settings",
];

test.describe("mobile layout", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  // 14 routes on the unbundled dev server: allow for first-paint cost.
  test.describe.configure({ timeout: 240_000 });

  for (const theme of ["light", "dark"] as const) {
    test(`no route scrolls horizontally at 390 px (${theme})`, async ({ loginAs }) => {
      const page = await loginAs(PERSONAS.ana);
      await page.addInitScript((value) => window.localStorage.setItem("fh.theme", value), theme);
      const overflowing: string[] = [];
      for (const route of ROUTES) {
        await page.goto(route);
        await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
        const [scrollWidth, innerWidth] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
        if (scrollWidth > innerWidth) overflowing.push(`${route}: ${scrollWidth} > ${innerWidth}`);
      }
      expect(overflowing).toEqual([]);
    });
  }

  test("the sign-in screen fits 390 px", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: /entrar/i })).toBeVisible();
    const [scrollWidth, innerWidth] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
  });
});
