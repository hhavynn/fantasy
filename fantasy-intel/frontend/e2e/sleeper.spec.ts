import { expect, test, type Page } from "@playwright/test";
import { ALL_NAV_PAGES } from "../lib/navigation";

// A Sleeper league on every page (multi-league Phase 4b; scripts/build_e2e_sleeper.py): HTTP 200, one h1, no backend
// or render error, no console error, and no ESPN anywhere in the page: a Sleeper league's projections are Fantasy
// Intel's own (Ours) and its records, statuses and transactions are Sleeper's. Team 1 is the viewer's, 201 "Echo RB".
const ROUTES = [...ALL_NAV_PAGES.map((p) => p.href), "/managers/2", "/players?id=201", "/data"];

const FAILURE_TEXT = /Can't reach the backend|The backend returned an error|database schema is out of date|Something went wrong|This page couldn't be rendered/;
// Pages that compare Ours with ESPN's own projections (owner research) say they do not apply to this league.
const ESPN_ALLOWED = new Set(["/projection-performance"]);

const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}

for (const route of ROUTES) {
  test(`sleeper ${route}`, async ({ page }) => {
    await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
    const errors = collectErrors(page);
    const res = await page.goto(route, { waitUntil: "load" });
    expect(res?.status(), "HTTP status").toBe(200);
    await page.waitForLoadState("networkidle");
    await expect(page.locator("h1"), "exactly one h1").toHaveCount(1);
    await expect(page.locator("main"), "no backend or render error").not.toContainText(FAILURE_TEXT);
    if (!ESPN_ALLOWED.has(route.split("?")[0])) {
      const text = await page.locator("body").innerText();
      const hits = text.split("\n").filter((line) => /\bESPN\b/.test(line));
      expect(hits, "no ESPN wording on a Sleeper league's page").toEqual([]);
    }
    expect(errors, "console errors").toEqual([]);
  });
}
