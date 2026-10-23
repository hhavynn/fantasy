import { expect, test, type Page } from "@playwright/test";
import { ALL_NAV_PAGES } from "../lib/navigation";
import { THEME_KEY } from "../lib/theme";

// Route smoke test for the PRODUCT.md quality bar: every page renders with HTTP 200, one h1, no
// console errors, no backend or render error state and no horizontal page scroll, in both themes.
// The viewport (1440 and 375) comes from the Playwright project. Ids are from the synthetic league
// (scripts/build_e2e_db.py): team 1 is a rival manager, 201 is "Echo RB".
const ROUTES = [
  ...ALL_NAV_PAGES.map((p) => p.href),
  "/managers/1",
  "/players?id=201",
  "/data",
  // Multi-league prototype: with MULTILEAGUE_UI_ENABLED unset (as here) it renders a "not enabled" notice
  // and disabled provider cards without calling the backend, whose /v2 routes are off in this setup.
  "/leagues",
  // Sign in and Create account: with sign-in off here they say so (e2e/signup.spec.ts covers them switched on).
  "/login",
  "/signup",
  // Research lab (synthetic pool from backend/tests/synthetic_research.py): each position and chart view,
  // a selected player with a comparison, and a filter that empties the pool.
  "/research/wr",
  "/research/rb?view=workload&window=last5",
  "/research/te?view=momentum",
  "/research/rb?sel=00-SYNRB01&cmp=00-SYNRB02&window=last3",
  "/research/wr?vol=40",
];

const THEMES = ["light", "dark"] as const;

// Titles of BackendProblem and the route error boundary: a page that shows one did not render its data.
const FAILURE_TEXT = /Can't reach the backend|The backend returned an error|database schema is out of date|Something went wrong|This page couldn't be rendered/;

// Player photos come from ESPN's CDN. Serve a 1x1 PNG instead so the test stays offline and a
// synthetic player id with no ESPN headshot doesn't log a 404 as a console error.
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
  for (const theme of THEMES) {
    test(`${route} (${theme})`, async ({ page }) => {
      await page.addInitScript(([key, value]) => {
        window.localStorage.setItem(key, value);
      }, [THEME_KEY, theme] as const);
      await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
      const errors = collectErrors(page);

      const res = await page.goto(route, { waitUntil: "load" });
      expect(res?.status(), "HTTP status").toBe(200);
      await page.waitForLoadState("networkidle");

      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.locator("h1"), "exactly one h1").toHaveCount(1);
      await expect(page.locator("main"), "no backend or render error").not.toContainText(FAILURE_TEXT);

      const overflow = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
      }));
      expect(overflow.scroll, `horizontal scroll at ${overflow.client}px`).toBeLessThanOrEqual(overflow.client);

      expect(errors, "console errors").toEqual([]);
    });
  }
}
