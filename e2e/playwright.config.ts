import { defineConfig, devices } from "@playwright/test";

const CI = Boolean(process.env.CI);

export default defineConfig({
  testDir: "./tests",
  globalSetup: "./global-setup.ts",
  outputDir: "./test-results",
  // Journeys share one database and the demo workspace; they only create uniquely named data (support/data.ts).
  fullyParallel: true,
  workers: 2,
  // One retry only in CI: a flaky journey must be fixed, not retried (docs/TESTING.md).
  retries: CI ? 1 : 0,
  forbidOnly: CI,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    // The compose `web` service (Vite proxies /v1 to the API, so the session cookie stays same-origin).
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  // The console runs on the Vite dev server (unbundled modules over a bind mount), so first paints can be slow.
  expect: { timeout: 15_000 },
  timeout: 90_000,
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
