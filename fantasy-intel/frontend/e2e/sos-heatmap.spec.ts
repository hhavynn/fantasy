import { expect, test, type Page } from "@playwright/test";

// Strength-of-schedule heatmap (issue #188) against the synthetic league (scripts/build_e2e_db.py): every defense has
// only two presumed-complete games, so no matchup may be graded (no rank, no difference), each cell shows its sample
// against the three games needed, and the source is still named because the defenses have values. Position, horizon,
// rows and the NFL team filter live in the URL; rows link to the profile, waivers and the trade analyzer. Runs on
// desktop-1440 and mobile-375.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

// loading.tsx streams the same h1 and a skeleton table first, so "loaded" is the grid's own table (its Avg rank
// column), never the heading.
const grid = (page: Page) => page.getByRole("table").filter({ has: page.getByRole("columnheader", { name: /Avg rank/ }) });
const rows = (page: Page) => grid(page).locator("tbody tr");

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText("Strength of schedule", { timeout: 20_000 });
  await expect(grid(page), "grid still streaming").toBeVisible({ timeout: 30_000 });
}

async function noSideScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [document.scrollingElement!.scrollWidth, window.innerWidth]);
  expect(scroll, "no horizontal page scroll").toBeLessThanOrEqual(width);
}

// On phones the rows chips and the filter form sit in a collapsed "Filters" disclosure.
async function openFilters(page: Page) {
  const summary = page.locator("main details.filter-box > summary");
  const closed = (await page.locator("main details.filter-box").getAttribute("open")) === null;
  if (closed && await summary.isVisible()) await summary.click();
}

const weekHeaders = async (page: Page) =>
  (await grid(page).locator("thead th").filter({ hasText: /^Wk \d+/ }).allTextContents()).map((t) => t.match(/^Wk \d+/)?.[0]);

test("under-sampled defenses get no grade: each cell shows its sample, the source and the baseline say why", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/schedule-intelligence/sos");
  await loaded(page);
  const main = page.locator("main");
  await expect(rows(page)).toHaveCount(32);
  await expect(main.getByText(/^Comparable only within QB: each defense is ranked on EPA allowed per eligible pass play/)).toBeVisible();
  await expect(main.getByText("0 of 384", { exact: true })).toBeVisible();      // graded matchups
  // Every week cell of the first row: an opponent, "not graded" and the games sampled out of the three needed.
  const cells = rows(page).first().locator("td[title^='Week']");
  await expect(cells).toHaveCount(12);
  for (const text of await cells.allInnerTexts()) {
    expect(text).toMatch(/(vs|@) [A-Z]{2,3}/);
    expect(text).toContain("? not graded");
    expect(text).toContain("2 of 3 g");
    expect(text).not.toMatch(/#\d/);
  }
  const title = await cells.first().getAttribute("title");
  expect(title).toMatch(/Not graded: only 2 presumed-complete game\(s\) sampled \(< 3\)\. Sample: 2 presumed-complete games, \d+ plays\./);
  // No average rank anywhere: missing, not 0.
  for (const avg of await rows(page).locator("td:nth-last-child(2)").allInnerTexts()) expect(avg.trim()).toBe("—");
  await expect(main.getByText(/Source: nflverse play-by-play aggregated per defense and week/)).toBeVisible();
  await expect(grid(page).locator("tfoot")).toContainText("Neutral baseline");
  const body = (await main.innerText()).toLowerCase();
  for (const word of ["fantasypros", "probability"]) expect(body, word).not.toContain(word);
  await noSideScroll(page);
  expect(errors).toEqual([]);
});

test("position and horizon toggle from the URL; playoff weeks come from the league settings", async ({ page }) => {
  await page.goto("/schedule-intelligence/sos");
  await loaded(page);
  expect(await weekHeaders(page)).toEqual(Array.from({ length: 12 }, (_, i) => `Wk ${i + 3}`));
  const main = page.locator("main");
  await main.getByRole("navigation", { name: "Horizon" }).getByRole("link", { name: "Fantasy playoffs" }).click();
  await expect(page).toHaveURL(/\/schedule-intelligence\/sos\?horizon=playoffs$/);
  await expect(grid(page).getByRole("columnheader", { name: /^Wk 15/ })).toBeVisible();
  expect(await weekHeaders(page)).toEqual(["Wk 15", "Wk 16", "Wk 17"]);
  await expect(main.getByText(/Fantasy playoffs \(weeks 15–17\)/)).toBeVisible();
  await expect(main.getByText(/Horizon weeks come from the league settings: regular season through week 14/)).toBeAttached();

  await main.getByRole("navigation", { name: "Position" }).getByRole("link", { name: "RB", exact: true }).click();
  await expect(page).toHaveURL(/pos=RB&horizon=playoffs$/);
  await expect(main.getByRole("heading", { name: "RB strength of schedule: fantasy playoffs" })).toBeVisible();
  await expect(main.getByText(/ranked on EPA allowed per designed rush · comparable only within RB/)).toBeVisible();
  await noSideScroll(page);
});

test("rows: NFL team filter, free agents to waivers, the roster to the trade analyzer, a player to his profile", async ({ page }) => {
  await page.goto("/schedule-intelligence/sos?pos=RB");
  await loaded(page);
  const main = page.locator("main");
  const filters = main.getByRole("form", { name: "Filters" });
  await openFilters(page);
  await filters.getByLabel("NFL team").selectOption("KC");
  await filters.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/pos=RB.*nfl=KC/);
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first().getByRole("rowheader")).toContainText("KC");
  await expect(rows(page).first().getByRole("link", { name: "RB waivers" })).toHaveAttribute("href", /^\/waivers\?pos=RB/);
  await expect(rows(page).first().getByRole("link", { name: "RB trade values" })).toHaveAttribute("href", "/trades/values?pos=RB");

  await openFilters(page);
  await main.getByRole("navigation", { name: "Rows" }).getByRole("link", { name: "Free agents" }).click();
  await expect(page).toHaveURL(/view=free_agents/);
  await expect(rows(page)).toHaveCount(1);
  const fa = rows(page).first();
  await expect(fa.getByRole("rowheader").getByRole("link")).toHaveAttribute("href", "/players?id=209");
  await expect(fa.getByRole("link", { name: /^Profile/ })).toHaveAttribute("href", "/players?id=209");
  await expect(fa.getByRole("link", { name: /^Add\/drop/ })).toHaveAttribute("href", "/waivers?add=209#add-drop");

  // The roster chip keeps the NFL team filter (none of Team 2's RBs plays for KC): clear it first.
  await page.goto("/schedule-intelligence/sos?pos=RB&view=free_agents");
  await loaded(page);
  await openFilters(page);
  await main.getByRole("navigation", { name: "Rows" }).getByRole("link", { name: "Roster" }).click();
  await expect(page).toHaveURL(/view=roster/);
  await expect(rows(page)).toHaveCount(4);                                        // Team 2's RBs (yours)
  const trade = rows(page).getByRole("link", { name: /^Trade away/ });
  await expect(trade).toHaveCount(4);
  await expect(trade.first()).toHaveAttribute("href", /^\/trades\?ta=2&a=\d+/);
  await expect(main.getByText(/Team 2 RB/).first()).toBeVisible();

  await openFilters(page);
  await filters.getByLabel("Player").selectOption({ label: "Juliet RB" });
  await filters.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/player=\d+/);
  await expect(rows(page)).toHaveCount(1);
  await rows(page).first().getByRole("link", { name: /^Profile/ }).click();
  await expect(page).toHaveURL(/\/players\?id=\d+/);
});
