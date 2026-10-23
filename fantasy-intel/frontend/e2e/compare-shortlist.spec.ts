import { expect, test, type Locator, type Page } from "@playwright/test";

// Issue #175: the shortlist start-decision workspace on /compare, against the synthetic league
// (scripts/build_e2e_db.py, current week 3): 201 "Echo RB" (ESPN 10.3), 401 "Uniform TE" (7.3), 301 "Mike WR" (5.3)
// and 101 "Alpha QB" (8.3, not FLEX-eligible). Runs on desktop-1440 and mobile-375 with touch, so phones tap.
// Every wait is for real content (the verdict, the table, the URL), never the loading skeleton's heading.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.use({ hasTouch: true });

const isPhone = () => /^mobile/.test(test.info().project.name);
const FOUR = "/compare?ids=201,401,301,101&slot=FLEX";

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function press(locator: Locator) {
  if (isPhone()) await locator.tap();
  else await locator.click();
}

async function noSidewaysScroll(page: Page) {
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
}

const grid = (page: Page) => page.getByRole("table", { name: /Shortlist of 4 players/ });

test("the two-player URL still opens the pair view, and leads into the shortlist", async ({ page }) => {
  await page.goto("/compare?a=201&b=401");
  await expect(page.getByRole("region", { name: "Projection comparison" })).toBeVisible({ timeout: 20_000 });
  const modes = page.getByRole("navigation", { name: "Comparison view" });
  await expect(modes.getByRole("link", { name: "Two players" })).toHaveAttribute("aria-current", "page");
  await press(modes.getByRole("link", { name: /Shortlist/ }));
  await expect(page).toHaveURL(/\/compare\?ids=201,401$/);
  await expect(page.getByRole("heading", { level: 2, name: /Echo RB is best projected for week 3/ })).toBeVisible({ timeout: 20_000 });
});

test("four players for FLEX: best projected, N/A not zero, no sideways page scroll", async ({ page }) => {
  await page.goto(FOUR);
  await expect(page.getByRole("heading", { level: 2, name: "Echo RB is best projected at FLEX for week 3" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Alpha QB (QB) cannot start at FLEX.")).toBeVisible();
  const table = grid(page);
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader")).toHaveCount(5);
  const eligibility = table.getByRole("row", { name: /Can start at FLEX/ });
  await expect(eligibility.getByRole("cell")).toHaveText(["Yes", "Yes", "Yes", "No"]);
  // a quarterback's target share does not apply: N/A, never 0
  const targetShare = table.getByRole("row", { name: /^Target share/ });
  await expect(targetShare.getByRole("cell").nth(3)).toContainText("N/A");
  await expect(targetShare.getByRole("cell").nth(3)).not.toContainText("0");
  await expect(page.getByRole("navigation", { name: "Lineup slot" }).getByRole("link", { name: "FLEX", exact: true })).toHaveAttribute("aria-current", "page");
  await noSidewaysScroll(page);
});

test("the start pick is the viewer's, and any pair opens the full comparison with a way back", async ({ page }) => {
  await page.goto(FOUR);
  const players = page.getByRole("region", { name: "Players" });
  await expect(players.getByRole("link", { name: "Mark as my start for Mike WR" })).toBeVisible({ timeout: 20_000 });
  await press(players.getByRole("link", { name: "Mark as my start for Mike WR" }));
  await expect(page).toHaveURL(/start=301/);
  await expect(page.getByText("Your pick, Mike WR, is projected 5.0 points below the best projected (ESPN).")).toBeVisible();
  // the headline still names who is best projected, never who to start
  await expect(page.getByRole("heading", { level: 2, name: /Echo RB is best projected/ })).toBeVisible();

  await press(page.getByRole("region", { name: "Open a pair in the full comparison" }).getByRole("link", { name: "Echo RB vs Uniform TE" }));
  await expect(page).toHaveURL(/\/compare\?a=201&b=401&ids=201,401,301,101&slot=FLEX&start=301/);
  await expect(page.getByRole("region", { name: "Projection comparison" })).toBeVisible({ timeout: 20_000 });
  await press(page.getByRole("link", { name: /Back to the shortlist/ }));
  await expect(page).toHaveURL(/\/compare\?ids=201,401,301,101&slot=FLEX&start=301$/);
  await expect(grid(page)).toBeVisible({ timeout: 20_000 });
  await noSidewaysScroll(page);
});

test("add a player from search and remove one", async ({ page }) => {
  await page.goto("/compare?ids=201");
  await expect(page.getByRole("heading", { level: 2, name: "Add another player" })).toBeVisible({ timeout: 20_000 });
  const box = page.getByRole("combobox", { name: /Add a player \(1 of 4\)/ });
  await box.fill("Uniform");
  await press(page.getByRole("option", { name: /Uniform TE/ }).first());
  await expect(page).toHaveURL(/\/compare\?ids=201,401$/);
  await expect(grid(page).or(page.getByRole("table", { name: /Shortlist of 2 players/ }))).toBeVisible({ timeout: 20_000 });
  await press(page.getByRole("region", { name: "Players" }).getByRole("link", { name: "Remove Uniform TE" }));
  await expect(page).toHaveURL(/\/compare\?ids=201$/);
  await noSidewaysScroll(page);
});

// Review of #282 (Codex P2): the shortlist is team-scoped, so the rail's team, the page's "Analyzing" picker, the URL's
// `team=` and the team the API computed roster status for (the grid's data-team-id) always agree, in both directions
// and on first load. Team 2 is the owner default; Echo RB (201) is on team 1.
test("the rail, the page picker, the URL and the API agree on the shortlist's team", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1440", "the rail is the wide-desktop layout");
  await page.goto(FOUR);
  const table = grid(page);
  const rail = page.getByRole("complementary", { name: "Workspace" }).getByRole("combobox", { name: "Team" });
  const picker = page.getByRole("main").getByRole("combobox", { name: "Analyzing" });
  const echoRoster = table.getByRole("row", { name: /^Roster/ }).getByRole("cell").first();

  // first load, no team=: the default team in the rail is the team the API used
  await expect(table).toHaveAttribute("data-team-id", "2", { timeout: 20_000 });
  await expect(rail).toHaveValue("2");
  await expect(picker).not.toHaveValue(/team=/);
  await expect(echoRoster).toHaveText("Rostered");

  // rail → URL, page picker and API
  await rail.selectOption("1");
  await expect(page).toHaveURL(/\/compare\?ids=201%2C401%2C301%2C101&slot=FLEX&team=1$/);
  await expect(table).toHaveAttribute("data-team-id", "1", { timeout: 20_000 });
  await expect(picker).toHaveValue(/team=1/);
  await expect(rail).toHaveValue("1");
  await expect(echoRoster).toHaveText("Yours");

  // page picker → rail, URL and API (back to the default: no team= in the URL)
  await picker.selectOption({ label: "Team 2 (default)" });
  await expect(page).not.toHaveURL(/team=/);
  await expect(table).toHaveAttribute("data-team-id", "2", { timeout: 20_000 });
  await expect(rail).toHaveValue("2");
  await expect(echoRoster).toHaveText("Rostered");
  await picker.selectOption({ label: "Team 1" });
  await expect(page).toHaveURL(/team=1$/);
  await expect(rail).toHaveValue("1");
  await expect(table).toHaveAttribute("data-team-id", "1", { timeout: 20_000 });
});
