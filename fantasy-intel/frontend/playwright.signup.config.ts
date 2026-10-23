import { defineConfig, devices } from "@playwright/test";

// Sign in and Sign up (issue #270): the passwordless email-link pages against a synthetic Supabase Auth and API
// (e2e/signup-fake-server.mjs): no real email, no real accounts. Two `next start` servers share it, one with this app's
// SIGNUPS_OPEN unset (the default, as in production) and one with SIGNUPS_OPEN=1; each test sets the fake API's
// SIGNUPS_ENABLED answer. One worker, because the tests share the fake's state. Own ports, so it runs beside the other
// suites.
export const SIGNUP_E2E = {
  fake: "http://127.0.0.1:8771",
  closed: "http://127.0.0.1:3104",
  open: "http://127.0.0.1:3105",
} as const;

const web = (port: number, signupsOpen: string) => ({
  command: `npx next start -p ${port}`,
  url: `http://127.0.0.1:${port}/login`,
  env: {
    API_URL: SIGNUP_E2E.fake, API_KEY: "", SITE_PASSWORD: "", RATE_LIMIT_PER_MINUTE: "",
    MULTILEAGUE_UI_ENABLED: "1", CLASSIC_SITE_ENABLED: "0",
    SUPABASE_URL: SIGNUP_E2E.fake, SUPABASE_PUBLISHABLE_KEY: "e2e-publishable-key", SUPABASE_LOCAL: "1",
    SITE_URL: `http://127.0.0.1:${port}`, SIGNUPS_OPEN: signupsOpen,
  },
  timeout: 60_000,
  reuseExistingServer: false,
});

export default defineConfig({
  testDir: "./e2e",
  testMatch: /signup\.spec\.ts/,
  outputDir: "./test-results-signup",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report-signup" }]] : [["list"]],
  use: { trace: "retain-on-failure" },
  projects: [
    { name: "signup-desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "signup-mobile-375", use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 } } },
  ],
  webServer: [
    { command: "node e2e/signup-fake-server.mjs 8771", url: `${SIGNUP_E2E.fake}/health`, timeout: 30_000, reuseExistingServer: false },
    web(3104, ""),
    web(3105, "1"),
  ],
});
