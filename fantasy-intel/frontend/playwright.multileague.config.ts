import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The multi-league site's parity matrix (multi-league Phase 5): every page under /l/<lg>/ for an ESPN public league,
// an ESPN private league (signed in as a member) and a Sleeper league. scripts/e2e_multileague.py runs a throwaway
// Postgres, a fake Supabase JWKS and the API over three published league files; the web server verifies the same
// tokens. Own ports, so it runs beside the other smoke tests.
const API_PORT = 8767;
const AUTH_PORT = 8768;
const WEB_PORT = 3102;
export const E2E_DIR = path.resolve(__dirname, "../data/e2e-multileague");

export default defineConfig({
  testDir: "./e2e",
  testMatch: /multileague\.spec\.ts/,
  outputDir: "./test-results-multileague",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report-multileague" }]] : [["list"]],
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [
    { name: "multileague-desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: [
    {
      command: `uv run python ../scripts/e2e_multileague.py "${E2E_DIR}" ${API_PORT} ${AUTH_PORT}`,
      cwd: "../backend",
      url: `http://127.0.0.1:${API_PORT}/health`,
      timeout: 240_000,
      reuseExistingServer: false,
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/`,
      env: {
        API_URL: `http://127.0.0.1:${API_PORT}`, API_KEY: "", SITE_PASSWORD: "",
        MULTILEAGUE_UI_ENABLED: "1", SUPABASE_URL: `http://127.0.0.1:${AUTH_PORT}`,
        SUPABASE_PUBLISHABLE_KEY: "e2e-publishable-key", SUPABASE_LOCAL: "1", SIGNUPS_OPEN: "",
      },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
