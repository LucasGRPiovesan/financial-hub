import { test as base, expect, type Page } from "@playwright/test";

import { demoLogin } from "./support/api";
import { PERSONAS, type Persona } from "./support/personas";

interface Fixtures {
  /**
   * Opens a page already signed in as `persona`, via the API (fast, no UI): POST /v1/auth/demo-login, then the
   * session cookie is carried into a fresh browser context (storageState). One session per call, so a journey
   * that signs out never invalidates another test's session. Use the UI picker (pages/login-page.ts) only in
   * journeys that are about signing in.
   */
  loginAs: (persona: Persona) => Promise<Page>;
}

export const test = base.extend<Fixtures>({
  loginAs: async ({ browser, baseURL }, use) => {
    const contexts: Array<{ close(): Promise<void> }> = [];
    await use(async (persona) => {
      const api = await demoLogin(baseURL ?? "http://localhost:5173", persona);
      const storageState = await api.storageState();
      await api.dispose();
      const context = await browser.newContext({ baseURL, storageState });
      contexts.push(context);
      return context.newPage();
    });
    await Promise.all(contexts.map((context) => context.close()));
  },
});

export { expect, PERSONAS };
