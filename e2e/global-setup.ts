import { chromium, type FullConfig } from "@playwright/test";

import { demoLogin } from "./support/api";
import { PERSONAS } from "./support/personas";

/**
 * Warms the Vite dev server: its first request for each route compiles modules and may re-optimise
 * dependencies (a full reload mid-test). Visiting the screens once, with a generous timeout, keeps that
 * cost out of the journeys, so they need no sleeps or inflated timeouts.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL;
  if (!baseURL) throw new Error("baseURL is not configured (E2E_BASE_URL)");

  const api = await demoLogin(baseURL, PERSONAS.ana);
  const storageState = await api.storageState();
  await api.dispose();

  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ baseURL, storageState });
    const page = await context.newPage();
    page.setDefaultTimeout(60_000);
    for (const path of ["/", "/projects"]) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
    }
    await page.getByRole("link", { name: "API keys", exact: true }).first().click();
    await page.waitForLoadState("networkidle");
    await context.close();
    // Also warm the signed-out screen.
    const anonymous = await browser.newContext({ baseURL });
    const login = await anonymous.newPage();
    login.setDefaultTimeout(60_000);
    await login.goto("/login");
    await login.waitForLoadState("networkidle");
    await anonymous.close();
  } finally {
    await browser.close();
  }
}
