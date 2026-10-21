import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// Browser smoke test of a Sleeper league (multi-league Phase 4b): e2e/sleeper.spec.ts against the FastAPI backend
// serving the synthetic Sleeper league built by scripts/build_e2e_sleeper.py (half-PPR, superflex, TE premium; its
// projections are Fantasy Intel's own). Own ports, so it can run beside the ESPN smoke test.
const API_PORT = 8766;
const WEB_PORT = 3101;
const E2E_DB = path.resolve(__dirname, "../data/e2e-sleeper.duckdb");

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
  testMatch: /sleeper\.spec\.ts/,
  outputDir: "./test-results-sleeper",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report-sleeper" }]] : [["list"]],
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [
    { name: "sleeper-desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "sleeper-mobile-375", use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 } } },
  ],
  webServer: [
    {
      command: `uv run python ../scripts/build_e2e_sleeper.py "${E2E_DB}" > /dev/null && uv run uvicorn app.main:app --port ${API_PORT} --log-level warning`,
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
