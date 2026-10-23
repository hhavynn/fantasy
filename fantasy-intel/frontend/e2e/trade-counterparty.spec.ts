import { expect, test, type Page } from "@playwright/test";

// Trade counterparty patterns on the manager scouting tab (issue #215) against the synthetic league
// (scripts/build_e2e_db.py). Team 4 made one trade (with team 1: one player sent, two received), below the
// three-trade threshold, so every breakdown is withheld with its denominator; team 3 made none (a recorded zero).
// Team 2 is the owner default, so team 4's page links into trade discovery with team 4 as the opponent.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const section = (page: Page) => page.locator("section").filter({ has: page.getByRole("heading", { name: "Trade counterparty patterns" }) });

async function open(page: Page, team: number) {
  await page.goto(`/managers/${team}?tab=scouting`);
  // Real content, not the loading skeleton: the section heading only exists once the report has rendered.
  await expect(page.getByRole("heading", { name: "Trade counterparty patterns" })).toBeVisible({ timeout: 30_000 });
}

async function noHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

test("a small sample shows its denominator and withholds the breakdown", async ({ page }) => {
  await open(page, 4);
  const s = section(page);
  await expect(s).toContainText("1 executed trade of 1 in the league");
  await expect(s).toContainText("A breakdown needs at least 3 trades");
  await expect(s).toContainText("Every breakdown is withheld: each needs at least 3 trades in its denominator.");
  await expect(s.locator("[data-pattern=trade_size]")).toHaveText(
    "Trade sizes (players sent-for-received): 1 (trades with a recorded player leg for this team)");
  await expect(s.locator("[data-pattern=partners]")).toHaveText("Trade partners: 1 (trades with a recorded partner team)");
  await expect(s.getByRole("table")).toHaveCount(0);           // nothing broken down below the threshold
  // the trades behind the counts stay attributable
  await s.getByText("The 1 trade behind these patterns").click();
  const trades = s.getByRole("region", { name: "Trades behind the patterns" });
  await expect(trades).toContainText("Team 1");
  await expect(trades).toContainText("1-for-2");
  await noHorizontalScroll(page);
});

test("the discovery link opens trade discovery with this manager as the opponent", async ({ page }) => {
  await open(page, 4);
  const link = section(page).getByRole("link", { name: "Any position" });
  await expect(link).toHaveAttribute("href", /\/trades\/discover\?mode=targeted&opponent=4/);
  await link.click();
  await expect(page).toHaveURL(/\/trades\/discover\?.*opponent=4/);
  await expect(page.getByRole("heading", { name: "Trade discovery", level: 1 })).toBeVisible({ timeout: 30_000 });
});

test("no recorded trade is an honest empty state", async ({ page }) => {
  await open(page, 3);
  const s = section(page);
  await expect(s.getByText("No executed trades recorded")).toBeVisible();
  await expect(s).toContainText("not a sign the manager will not trade");
  await noHorizontalScroll(page);
});
