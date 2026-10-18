import { expect, test, type Page } from "@playwright/test";

// Navigation and analysis-context journeys against the synthetic league (scripts/build_e2e_db.py): team 2 is the
// owner default, team 1 a rival, player 201 "Echo RB". The viewport comes from the Playwright project: desktop
// (1440) uses the section links and sub-nav, phones (375, 320) the bottom tab bar and the menu, the tablet (768)
// the desktop bar at its tightest. Selectors are roles and hrefs, so the markup can change without touching this.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
const FAILURE_TEXT = /Can't reach the backend|The backend returned an error|Something went wrong|This page couldn't be rendered/;

const isPhone = () => /^mobile/.test(test.info().project.name);

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

/** The primary section links for this viewport: the app bar on desktop and tablets, the bottom tab bar on phones. */
const sections = (page: Page) => page.getByRole("navigation", { name: "Sections" });
/** The current group's sub-nav. */
const subnav = (page: Page, group: string) => page.getByRole("navigation", { name: `${group} pages` });

async function loaded(page: Page) {
  await page.waitForLoadState("networkidle");
  // Streamed pages can sit on a loading skeleton after the network goes idle (Overview under load); its h1 holds
  // only a placeholder bar. Every skeleton is a busy status region, so wait until none is left.
  await expect(page.locator('main [role="status"][aria-busy="true"]'), "page still loading").toHaveCount(0, { timeout: 20_000 });
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("main")).not.toContainText(FAILURE_TEXT);
}

async function noSidewaysScroll(page: Page) {
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
}

test("every primary section is reachable from the section links and marks itself current", async ({ page }) => {
  await page.goto("/");
  await loaded(page);
  for (const [label, path, h1] of [
    ["Players", "/rankings", /Rankings/],
    ["Trades", "/trades", /Trade analyzer/],
    ["League", "/weekly-recap", /recap/i],
    ["My team", "/", /./],
  ] as const) {
    await sections(page).getByRole("link", { name: label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${path === "/" ? "/$" : path}`));
    await loaded(page);
    await expect(page.locator("h1")).toHaveText(h1);
    await expect(sections(page).getByRole("link", { name: label, exact: true })).toHaveAttribute("aria-current", "true");
  }
});

test("nested routes highlight their section and page", async ({ page }) => {
  for (const [path, group, pageLabel] of [
    ["/managers/1", "League", "Managers"],
    ["/trades/discover", "Trades", "Discover"],
    ["/research/wr", "Players", "Research lab"],
    ["/players?id=201", "Players", "Players"],
    ["/waivers", "My team", "Waivers"],
  ] as const) {
    await page.goto(path);
    await loaded(page);
    await expect(sections(page).getByRole("link", { name: group, exact: true }), path).toHaveAttribute("aria-current", "true");
    await expect(subnav(page, group).getByRole("link", { name: pageLabel, exact: true }), path).toHaveAttribute("aria-current", "page");
    await expect(subnav(page, group).locator("[aria-current]"), `${path}: one current page`).toHaveCount(1);
  }
});

test("the advanced Players tools sit in a labelled Research cluster of the sub-nav", async ({ page }) => {
  await page.goto("/movers");
  await loaded(page);
  const nav = subnav(page, "Players");
  await expect(nav).toContainText("Research");
  await expect(nav.getByRole("link", { name: "Projection movers" })).toHaveAttribute("aria-current", "page");
  await expect(nav.getByRole("link", { name: "Rankings" })).toBeVisible();
});

test("the analyzed team carries across team-scoped pages, survives league-wide pages and never leaks into them", async ({ page }) => {
  await page.goto("/start-sit?team=1");
  await loaded(page);
  await expect(page.getByRole("combobox", { name: "Analyzing" })).toHaveValue(/team=1/);

  await subnav(page, "My team").getByRole("link", { name: "Waivers", exact: true }).click();
  await expect(page).toHaveURL(/\/waivers\?team=1$/);
  await loaded(page);

  // A league-wide page: its own URL and links to other league-wide pages stay clean.
  await sections(page).getByRole("link", { name: "Players", exact: true }).click();
  await expect(page).toHaveURL(/\/rankings$/);
  await loaded(page);
  await expect(subnav(page, "Players").getByRole("link", { name: "Compare", exact: true })).toHaveAttribute("href", "/compare");

  // Back to a team-scoped page: still team 1, not the default. (Phones reopen the group's last page, Waivers.)
  await sections(page).getByRole("link", { name: "My team", exact: true }).click();
  await expect(page).toHaveURL(isPhone() ? /\/waivers\?team=1$/ : /\/\?team=1$/);
  await loaded(page);
  await expect(page.getByRole("link", { name: "Fantasy Intel home" })).toHaveAttribute("href", "/?team=1");
});

test("returning to the default team sticks across pages", async ({ page }) => {
  await page.goto("/waivers?team=1");
  await loaded(page);
  await page.getByRole("link", { name: "Back to default" }).click();
  await expect(page).toHaveURL(/\/waivers$/);
  await loaded(page);
  await page.goto("/rankings");
  await loaded(page);
  await expect(sections(page).getByRole("link", { name: "My team", exact: true })).toHaveAttribute("href", /^\/(?!.*team=)/);
});

test("Trade analyzer, Discover and Evaluate keep the analyzed team", async ({ page }) => {
  await page.goto("/trades/discover?team=1");
  await loaded(page);
  await expect(page.getByRole("link", { name: "Trade analyzer" }).first()).toHaveAttribute("href", "/trades?team=1");
  await page.goto("/trades?team=1");
  await loaded(page);
  await expect(page.getByRole("link", { name: "Discover trades" }).first()).toHaveAttribute("href", "/trades/discover?team=1");
});

test("the command palette opens team-scoped pages for the analyzed team and league-wide pages without it", async ({ page }) => {
  await page.goto("/?team=1");
  await loaded(page);
  await page.getByRole("button", { name: /Search players, pages/ }).click();
  const dialog = page.getByRole("dialog", { name: "Search" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "Search players and pages" })).toBeFocused();
  await page.keyboard.type("waiver");
  await expect(dialog.getByRole("option").first()).toContainText(/waiver/i);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/waivers\?team=1$/);
  await loaded(page);
  await expect(dialog).toBeHidden();

  await page.keyboard.press("Control+k");
  await expect(dialog).toBeVisible();
  await page.keyboard.type("rankings");
  await dialog.getByRole("option", { name: /Rankings/ }).first().click();
  await expect(page).toHaveURL(/\/rankings$/);
});

test("the command palette closes with Escape and returns focus to its trigger", async ({ page }) => {
  await page.goto("/rankings");
  await loaded(page);
  const trigger = page.getByRole("button", { name: /Search players, pages/ });
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "Search" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Search" })).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("player search failing degrades to pages and actions instead of breaking the palette", async ({ page }) => {
  await page.route("**/api/search?**", (r) => r.fulfill({ status: 500, body: "{}" }));
  await page.goto("/rankings");
  await loaded(page);
  await page.getByRole("button", { name: /Search players, pages/ }).click();
  await page.keyboard.type("echo");
  await expect(page.getByRole("dialog", { name: "Search" })).toContainText("Player search is unavailable right now");
});

test("back and forward restore each page with its team", async ({ page }) => {
  await page.goto("/matchup?team=1");
  await loaded(page);
  await subnav(page, "My team").getByRole("link", { name: "Start/Sit", exact: true }).click();
  await expect(page).toHaveURL(/\/start-sit\?team=1$/);
  await loaded(page);
  await page.goBack();
  await expect(page).toHaveURL(/\/matchup\?team=1$/);
  await loaded(page);
  await expect(subnav(page, "My team").getByRole("link", { name: "Matchup", exact: true })).toHaveAttribute("aria-current", "page");
  await page.goForward();
  await expect(page).toHaveURL(/\/start-sit\?team=1$/);
  await loaded(page);
});

test("deep links with filters and a team survive a reload", async ({ page }) => {
  for (const url of ["/trades/discover?team=1&mode=practical", "/rankings?pos=WR&horizon=ros", "/waivers?team=1"]) {
    await page.goto(url);
    await loaded(page);
    await page.reload();
    await loaded(page);
    expect(page.url(), url).toContain(url);
  }
});

test("an unknown or malformed team explains itself instead of failing", async ({ page }) => {
  await page.goto("/waivers?team=999");
  await expect(page.locator("main")).toContainText("isn't a team in this league");
  await page.goto("/?team=abc");
  await expect(page.locator("main")).toContainText("isn't a team in this league");
  await expect(page.locator("h1")).toHaveCount(1);
});

test("key pages fit the viewport without sideways scroll", async ({ page }) => {
  for (const path of ["/", "/rankings", "/start-sit", "/waivers", "/trades", "/trades/discover", "/players?id=201", "/movers"]) {
    await page.goto(path);
    await loaded(page);
    await test.step(path, () => noSidewaysScroll(page));
  }
});

test("desktop and tablet: the four section links are fully visible in the app bar", async ({ page }) => {
  test.skip(isPhone(), "phones use the tab bar");
  await page.goto("/trades");
  await loaded(page);
  const nav = page.locator(".nav-groups");
  const fits = await nav.evaluate((n) => n.scrollWidth <= n.clientWidth);
  expect(fits, "section links clipped").toBe(true);
  for (const label of ["My team", "Players", "Trades", "League"]) {
    await expect(sections(page).getByRole("link", { name: label, exact: true })).toBeInViewport();
  }
});

// ---------- Phones ----------

test("phone menu: opens, lists every page, closes with Escape, an outside tap and a link", async ({ page }) => {
  test.skip(!isPhone(), "the menu is the phone layout");
  await page.goto("/rankings");
  await loaded(page);
  const toggle = page.getByRole("button", { name: "Open menu" });
  await toggle.click();
  const menu = page.getByRole("navigation", { name: "All pages" });
  await expect(menu).toBeVisible();
  await expect(page.getByRole("button", { name: "Close menu" })).toHaveAttribute("aria-expanded", "true");
  for (const label of ["Overview", "Waivers", "Rankings", "Research lab", "Analyzer", "Playoff race", "Data quality"]) {
    await expect(menu.getByRole("link", { name: label, exact: true })).toBeVisible();
  }
  await expect(menu.getByRole("link", { name: "Rankings", exact: true })).toHaveAttribute("aria-current", "page");

  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(page.getByRole("button", { name: "Open menu" })).toBeFocused();

  await toggle.click();
  await expect(menu).toBeVisible();
  // A tap outside the menu (the tab bar's padding, not one of its links) closes it.
  const vp = page.viewportSize()!;
  await page.mouse.click(1, vp.height - 1);
  await expect(menu).toBeHidden();

  await toggle.click();
  await menu.getByRole("link", { name: "Playoff race", exact: true }).click();
  await expect(page).toHaveURL(/\/playoff-race/);
  await expect(menu).toBeHidden();
  await loaded(page);
});

test("phone menu closes when the browser goes back", async ({ page }) => {
  test.skip(!isPhone(), "the menu is the phone layout");
  await page.goto("/rankings");
  await loaded(page);
  await page.goto("/compare");
  await loaded(page);
  await page.getByRole("button", { name: "Open menu" }).click();
  await expect(page.getByRole("navigation", { name: "All pages" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/rankings$/);
  await expect(page.getByRole("navigation", { name: "All pages" })).toBeHidden();
});

test("phone menu sits between the app bar and the tab bar", async ({ page }) => {
  test.skip(!isPhone(), "the menu is the phone layout");
  await page.goto("/");
  await loaded(page);
  await page.getByRole("button", { name: "Open menu" }).click();
  const menu = await page.getByRole("navigation", { name: "All pages" }).boundingBox();
  const tabs = await page.locator(".tabbar").boundingBox();
  expect(menu && tabs && menu.y + menu.height <= tabs.y + 1, "menu overlaps the tab bar").toBe(true);
});

test("phone tabs reopen the last page used in each group, and the current tab returns to the group's start", async ({ page }) => {
  test.skip(!isPhone(), "the tab bar is the phone layout");
  await page.goto("/trades/discover?team=1");
  await loaded(page);
  await sections(page).getByRole("link", { name: "Players", exact: true }).click();
  await expect(page).toHaveURL(/\/rankings$/);
  await loaded(page);
  await page.getByRole("link", { name: "WR", exact: true }).first().click();
  await expect(page).toHaveURL(/pos=WR/);
  await loaded(page);

  // Trades remembers Discover (for team 1); Players, the current tab, goes back to its first page.
  await expect(sections(page).getByRole("link", { name: "Trades", exact: true })).toHaveAttribute("href", /^\/trades\/discover\?.*team=1/);
  await expect(sections(page).getByRole("link", { name: "Players", exact: true })).toHaveAttribute("href", "/rankings");

  await sections(page).getByRole("link", { name: "Trades", exact: true }).click();
  await expect(page).toHaveURL(/\/trades\/discover\?.*team=1/);
  await loaded(page);
  // Players remembers the WR filter.
  await expect(sections(page).getByRole("link", { name: "Players", exact: true })).toHaveAttribute("href", /\/rankings\?.*pos=WR/);
});

test("phone tab bar does not cover the end of the page", async ({ page }) => {
  test.skip(!isPhone(), "the tab bar is the phone layout");
  await page.goto("/rankings");
  await loaded(page);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const last = page.locator("main > *").last();
  const box = await last.boundingBox();
  const tabs = await page.locator(".tabbar").boundingBox();
  expect(box && tabs && box.y + box.height <= tabs.y + 1, "last section hidden under the tab bar").toBe(true);
});

test("a shared power-rankings card links back to the board for the card's team, not a stored one", async ({ page }) => {
  // Analyze the default team first, so a stored team exists that differs from the card's (team 1, a rival).
  await page.goto("/power-rankings?team=2");
  await loaded(page);
  await page.goto("/power-rankings/1");
  await loaded(page);
  const back = page.locator("main").getByRole("link", { name: /^Power rankings/ }).first();
  await expect(back).toHaveAttribute("href", /[?&]team=1(&|$)/);
});
