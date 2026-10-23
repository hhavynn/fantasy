import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// "Why it changed" on /movers (issue #225): e2e/movers-attribution.spec.ts against the FastAPI backend serving the
// Sleeper fixture league plus two stored Fantasy Intel forecast cycles with invented input terms
// (scripts/build_e2e_attribution.py). Own ports and database, so it runs beside the other browser suites and never
// changes their fixtures or goldens.
const API_PORT = 8772;
const WEB_PORT = 3106;
const E2E_DB = path.resolve(__dirname, "../data/e2e-attribution.duckdb");

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
  testMatch: /movers-attribution\.spec\.ts/,
  outputDir: "./test-results-attribution",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report-attribution" }]] : [["list"]],
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [
    { name: "desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile-375", use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 } } },
  ],
  webServer: [
    {
      command: `uv run python ../scripts/build_e2e_attribution.py "${E2E_DB}" > /dev/null && uv run uvicorn app.main:app --port ${API_PORT} --log-level warning`,
      cwd: "../backend",
      url: `http://127.0.0.1:${API_PORT}/health`,
      env: BACKEND_ENV,
      timeout: 180_000,
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
