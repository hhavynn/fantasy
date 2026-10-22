import { defineConfig, devices } from "@playwright/test";
import base from "./playwright.config";

// Visual regression for a few core destinations (e2e/visual.spec.ts; issue #202) against the same synthetic league
// and servers as the route smoke (playwright.config.ts), one step after it in CI. Baselines live in
// e2e/visual-baselines/<project>/ and are only ever written by the "Visual baselines" workflow
// (.github/workflows/visual-baselines.yml) on CI's browser build, then reviewed and committed by a person:
// docs/qa/release-review.md. Nothing here writes a baseline unless --update-snapshots is passed explicitly.
const phone = { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } };
export default defineConfig({
  ...base,
  testIgnore: undefined,
  testMatch: /visual\.spec\.ts/,
  outputDir: "./test-results-visual",
  snapshotPathTemplate: "{testDir}/visual-baselines/{projectName}/{arg}{ext}",
  updateSnapshots: "none",
  // One retry in CI: toHaveScreenshot already waits for two identical frames, so a retry only absorbs a slow paint.
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report-visual" }]] : [["list"]],
  expect: {
    toHaveScreenshot: { animations: "disabled", caret: "hide", scale: "css", threshold: 0.2, maxDiffPixelRatio: 0.01 },
  },
  use: { ...base.use, timezoneId: "UTC", locale: "en-US", colorScheme: "dark" },
  projects: [
    { name: "visual-mobile-390", use: phone },
    { name: "visual-tablet-768", use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } } },
    { name: "visual-desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
});
