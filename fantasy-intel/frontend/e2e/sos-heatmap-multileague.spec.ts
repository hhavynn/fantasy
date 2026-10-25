import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

// Strength-of-schedule heatmap (issue #188) on the multi-league site (playwright.multileague.config.ts): the grid comes
// from the scoped /v2 API, and every chip, form and decision link stays in the league of the page (/l/<lg>/...), with
// the analyzed team kept on the trade analyzer link. The ESPN fixture league (scripts/build_e2e_db.py) has teams 1-4.
type Manifest = { leagues: Record<"espn_public" | "sleeper", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

test("the grid, its filters and its decision links stay in the league and keep the analyzed team", async ({ page }) => {
  const { espn_public: lg } = manifest().leagues;
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`/l/${lg}/schedule-intelligence/sos?pos=RB&view=roster&team=1`);
  // loading.tsx streams the same h1 and a skeleton table first: wait for the grid's own column.
  const grid = page.getByRole("table").filter({ has: page.getByRole("columnheader", { name: /Avg rank/ }) });
  await expect(grid).toBeVisible({ timeout: 30_000 });
  await expect(grid.locator("tbody tr")).toHaveCount(3);                         // Team 1's RBs
  const main = page.locator("main");
  await expect(main.getByText(/Team 1 RB/).first()).toBeVisible();

  const trade = grid.getByRole("link", { name: /^Trade away/ });
  await expect(trade).toHaveCount(3);
  for (const href of await trade.evaluateAll((as) => as.map((a) => a.getAttribute("href")))) {
    expect(href).toMatch(new RegExp(`^/l/${lg}/trades\\?`));
    const q = new URL(href!, "http://x").searchParams;
    expect(q.get("ta"), href!).toBe("1");
    expect(q.get("team"), href!).toBe("1");
  }
  for (const href of await grid.locator("a").evaluateAll((as) => as.map((a) => a.getAttribute("href")))) {
    expect(href, "every in-app link is league-scoped").toMatch(new RegExp(`^(/l/${lg}/|\\?)`));
  }
  for (const href of await main.locator("nav.filters a").evaluateAll((as) => as.map((a) => a.getAttribute("href")))) {
    expect(href, "filter chips stay in the league").toMatch(new RegExp(`^/l/${lg}/schedule-intelligence/sos`));
  }
  await expect(main.getByRole("form", { name: "Filters" })).toHaveAttribute("action", `/l/${lg}/schedule-intelligence/sos`);

  await main.getByRole("navigation", { name: "Horizon" }).getByRole("link", { name: "Fantasy playoffs" }).click();
  await expect(page).toHaveURL(new RegExp(`/l/${lg}/schedule-intelligence/sos\\?.*horizon=playoffs`));
  await expect(grid.getByRole("columnheader", { name: /^Wk 15/ })).toBeVisible();
  await trade.first().click();
  await expect(page).toHaveURL(new RegExp(`/l/${lg}/trades\\?.*ta=1`));
  expect(errors).toEqual([]);
});
