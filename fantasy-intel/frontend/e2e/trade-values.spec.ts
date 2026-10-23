import { expect, test, type Page } from "@playwright/test";

// Trade values board (issue #184) against the synthetic league (scripts/build_e2e_db.py): one stored ESPN projection
// snapshot, so the week-over-week change is unavailable everywhere (never a 0). Every value is labelled a Fantasy
// Intel model estimate; position and owner filters and sorting live in the URL; a player's calculation links to his
// profile, Compare, his owner and the trade analyzer. Runs on desktop-1440 and mobile-375.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText("Trade values", { timeout: 20_000 });
}

const rows = (page: Page) => page.locator("main table").first().locator("tbody tr");

async function noSideScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [document.scrollingElement!.scrollWidth, window.innerWidth]);
  expect(scroll, "no horizontal page scroll").toBeLessThanOrEqual(width);
}

test("values are labelled model estimates, change is unavailable (never 0), and filters and sorting use the URL", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/trades/values");
  await loaded(page);
  const main = page.locator("main");
  await expect(main.getByText("Model estimates, not trade prices")).toBeVisible();
  await expect(main.getByText("Week-over-week change unavailable")).toBeVisible();
  await expect(main.getByText(/Fantasy Intel model estimate · ESPN weekly projections · weeks 3–17 · calculation tv-1/)).toBeVisible();
  const n = await rows(page).count();
  expect(n).toBeGreaterThan(10);
  // The Change column (6th) says Unavailable on every row: no number, no 0.0.
  const changes = await rows(page).locator("td:nth-child(6)").allInnerTexts();
  expect(changes.length).toBe(n);
  for (const c of changes) expect(c.trim()).toBe("Unavailable");
  const body = (await main.innerText()).toLowerCase();
  for (const word of ["fantasypros", "acceptance probability", "will accept"]) expect(body, word).not.toContain(word);
  await noSideScroll(page);

  await main.getByRole("navigation", { name: "Position" }).getByRole("link", { name: "QB", exact: true }).click();
  await expect(page).toHaveURL(/\/trades\/values\?pos=QB$/);
  await expect(rows(page)).toHaveCount(4);
  await main.getByRole("link", { name: /Owner/ }).first().click();
  await expect(page).toHaveURL(/sort=owner/);
  await expect(page).toHaveURL(/pos=QB/);

  const owner = main.getByRole("form", { name: "Owner" });
  await owner.getByLabel("Owner").selectOption({ label: "Team 1" });
  await owner.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/owner=1/);
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first()).toContainText("Team 1");
  expect(errors).toEqual([]);
});

test("a player's calculation shows the week-by-week sum, version, caveats and links to profile, Compare, owner and analyzer", async ({ page }) => {
  await page.goto("/trades/values?pos=WR&sort=value&dir=desc");
  await loaded(page);
  const first = rows(page).first();
  await first.getByRole("link", { name: /^Details/ }).click();
  await expect(page).toHaveURL(/player=\d+/);
  const calc = page.locator("#calculation");
  await expect(calc.getByRole("heading", { name: /value is calculated/ })).toBeVisible();
  await expect(calc.getByText("Not a trade price.", { exact: false }).first()).toBeVisible();
  await expect(calc.getByText(/ESPN weekly projections/).first()).toBeVisible();
  await expect(calc.locator("table tbody tr")).toHaveCount(15);
  await expect(calc.getByText(/Why it moved/)).toBeVisible();
  await expect(calc.getByRole("link", { name: "Profile" })).toHaveAttribute("href", /^\/players\?id=\d+$/);
  await expect(calc.getByRole("link", { name: /^Compare/ })).toHaveAttribute("href", /^\/compare\?/);
  await expect(calc.getByRole("link", { name: /^Owner: / })).toHaveAttribute("href", /^\/managers\/\d+$/);
  const trade = calc.getByRole("link", { name: /in the analyzer$/ });
  await expect(trade).toHaveAttribute("href", /^\/trades\?ta=\d+/);
  await noSideScroll(page);
  await trade.click();
  await expect(page).toHaveURL(/\/trades\?ta=\d+/);
  await expect(page.locator("main h1:visible")).toHaveText("Trade analyzer", { timeout: 30_000 });
});

test("the board is in the Trades navigation", async ({ page }) => {
  await page.goto("/trades");
  await expect(page.locator("main h1:visible")).toHaveText("Trade analyzer", { timeout: 20_000 });
  const link = page.locator("a:visible[href='/trades/values']").first();
  await expect(link).toBeVisible();
  await link.click();
  await loaded(page);
});
