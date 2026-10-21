import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// Browser smoke test (e2e/): every route against the FastAPI backend serving the synthetic DuckDB
// built by scripts/build_e2e_db.py. Needs a production build first (`npm run build`). Both servers
// run on their own ports so a local dev server or backend on 3000/8000 is left alone.
const API_PORT = 8765;
const WEB_PORT = 3100;
const E2E_DB = path.resolve(__dirname, "../data/e2e.duckdb");

// Blank values shadow anything in the repo-root .env or frontend .env.local: no hosted-mode key,
// password or MotherDuck, and the fixture season instead of the clock's.
const BACKEND_ENV = {
  FANTASY_DB_PATH: E2E_DB,
  ESPN_SEASON: "2026",
  NFL_SEASON: "2026",
  ESPN_MY_TEAM_ID: "",
  FANTASY_API_KEY: "",
  MOTHERDUCK_DATABASE: "",
  EXTERNAL_USAGE_PREFERRED_PROVIDER: "",
};

export default defineConfig({
  testDir: "./e2e",
  testIgnore: /sleeper\.spec\.ts/,     // the Sleeper league runs from playwright.sleeper.config.ts
  outputDir: "./test-results",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile-375", use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 } } },
    // Navigation journeys only (not the full route smoke): the smallest phone and a tablet at the desktop bar's tightest.
    { name: "mobile-320", testMatch: /navigation\.spec\.ts/, use: { ...devices["Desktop Chrome"], viewport: { width: 320, height: 640 } } },
    { name: "tablet-768", testMatch: /navigation\.spec\.ts/, use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } } },
  ],
  webServer: [
    {
      command: `uv run python ../scripts/build_e2e_db.py "${E2E_DB}" > /dev/null && uv run uvicorn app.main:app --port ${API_PORT} --log-level warning`,
      cwd: "../backend",
      url: `http://127.0.0.1:${API_PORT}/health`,
      env: BACKEND_ENV,
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/`,
      env: { API_URL: `http://127.0.0.1:${API_PORT}`, API_KEY: "", SITE_PASSWORD: "" },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
