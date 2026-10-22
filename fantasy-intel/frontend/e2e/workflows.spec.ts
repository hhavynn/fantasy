import { expect, test, type Page } from "@playwright/test";

// Redesigned manager workflows against the synthetic league (scripts/build_e2e_db.py): team 2 is the owner
// default, team 1 a rival; 201 is "Echo RB" (team 1), 206 "Juliet RB" and 307 "Sierra WR" (team 2), 202 "Foxtrot RB"
// (team 1). Runs on the desktop-1440 and mobile-375 projects with touch enabled, so phone flows use real taps.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.use({ hasTouch: true });

const isPhone = () => /^mobile/.test(test.info().project.name);

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText(/\S/, { timeout: 20_000 });
}

/** Tap on phones, click on desktop: the same control either way. */
async function press(page: Page, locator: ReturnType<Page["locator"]>) {
  if (isPhone()) await locator.tap();
  else await locator.click();
}

async function noSidewaysScroll(page: Page) {
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
}

// ---------- Trades ----------

test("trade builder: pick players on both sides, see them named, and evaluate", async ({ page }) => {
  await page.goto("/trades");
  await loaded(page);
  const sideA = page.getByRole("region", { name: /^Team A sends/ });
  const sideB = page.getByRole("region", { name: /^Team B sends/ });

  if (isPhone()) {
    // One roster at a time behind the two-team switch.
    await expect(sideA).toBeVisible();
    await expect(sideB).toBeHidden();
  } else {
    await expect(sideA).toBeVisible();
    await expect(sideB).toBeVisible();
  }

  await press(page, sideA.locator("label").filter({ hasText: "Juliet RB" }));
  await press(page, sideA.locator("label").filter({ hasText: "Sierra WR" }));
  if (isPhone()) {
    const switchB = page.getByRole("button", { name: /Team B sends/ });
    await press(page, switchB);
    await expect(switchB).toHaveAttribute("aria-pressed", "true");
    await expect(sideA).toBeHidden();
    await expect(sideB).toBeVisible();
    // The switch keeps count of the side that is off screen.
    await expect(page.getByRole("button", { name: /Team A sends/ })).toContainText("2");
  }
  await press(page, sideB.locator("label").filter({ hasText: "Foxtrot RB" }));

  // The sticky bar names everyone in the trade, whichever roster is on screen.
  const bar = page.getByRole("region", { name: "Trade summary" });
  await expect(bar).toContainText("Juliet RB, Sierra WR");
  await expect(bar).toContainText("Foxtrot RB");
  await expect(bar).toBeInViewport();

  await press(page, bar.getByRole("button", { name: "Evaluate" }));
  await expect(page).toHaveURL(/ta=2&tb=1&a=206&a=307&b=202/);
  // The verdict band leads the result: a headline about both teams, never a grade.
  await expect(page.getByRole("heading", { level: 2, name: /Team 2 .*Team 1|Both lineups|Neither lineup/ })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("main")).not.toContainText(/acceptance probability:|grade:/i);
  await noSidewaysScroll(page);
});

// ---------- Rankings ----------

test("rankings: a dense list on phones with sort shortcuts and collapsible filters", async ({ page }) => {
  await page.goto("/rankings");
  await loaded(page);
  if (isPhone()) {
    const list = page.getByRole("list", { name: "Player rankings" });
    await expect(list).toBeVisible();
    // Dense rows: the first several players fit on one phone screen, not one tall card each.
    const rows = list.locator(":scope > li");
    const first = await rows.nth(0).boundingBox();
    const fourth = await rows.nth(3).boundingBox();
    expect(first && fourth && fourth.y - first.y < 420, "rows should be compact").toBe(true);

    // Filters sit behind a one-line summary that says what is selected.
    const summary = page.locator("details.filter-box > summary").first();
    await expect(summary).toContainText("This week");
    await press(page, summary);
    await expect(page.getByRole("navigation", { name: "Time window" })).toBeVisible();

    await press(page, page.getByRole("navigation", { name: "Sort by" }).getByRole("link", { name: /Rating/ }));
    await expect(page).toHaveURL(/sort=rating&dir=desc/);
  } else {
    await expect(page.getByRole("table", { name: "Player rankings" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Time window" })).toBeVisible();
  }
  await noSidewaysScroll(page);
});

// ---------- Waivers ----------

test("waivers: the claim verdict comes first and the position filter narrows the list", async ({ page }) => {
  await page.goto("/waivers");
  await loaded(page);
  await expect(page.getByRole("region", { name: "Top claims" }).getByRole("heading", { level: 2 })).toBeVisible();
  if (isPhone()) await press(page, page.locator("details.filter-box > summary").first());
  await press(page, page.getByRole("navigation", { name: "Player positions" }).getByRole("link", { name: "RB", exact: true }));
  await expect(page).toHaveURL(/pos=RB/);
  await loaded(page);
  await expect(page.locator("details.filter-box > summary").first()).toContainText("RB");
  await noSidewaysScroll(page);
});

// ---------- Players ----------

test("player search to profile to compare", async ({ page }) => {
  await page.goto("/rankings");
  await loaded(page);
  await press(page, page.getByRole("button", { name: /Search players, pages/ }));
  const dialog = page.getByRole("dialog", { name: "Search" });
  await expect(dialog).toBeVisible();
  await page.keyboard.type("Echo");
  await dialog.getByRole("option", { name: /Echo RB/ }).first().click();
  await expect(page).toHaveURL(/\/players\?id=201/);
  await loaded(page);
  await expect(page.locator("h1")).toContainText("Echo RB");

  await page.locator("main").getByRole("link", { name: /Compare Echo RB/ }).first().click();
  await expect(page).toHaveURL(/\/compare\?a=201/);
  await loaded(page);
  await noSidewaysScroll(page);
});

// ---------- Matchup ----------

test("matchup: phones compare both lineups slot by slot in one list", async ({ page }) => {
  test.skip(!isPhone(), "the paired list is the phone layout");
  await page.goto("/matchup");
  await loaded(page);
  const pairs = page.getByRole("list", { name: /against .*slot by slot/ });
  await expect(pairs).toBeVisible();
  // Header row, one row per starting slot, and the totals.
  expect(await pairs.locator(":scope > li").count()).toBeGreaterThan(4);
  await expect(pairs).toContainText("Total");
  await noSidewaysScroll(page);
});

// ---------- Explanations ----------

test("terms on a page are explained without hovering", async ({ page }) => {
  await page.goto("/start-sit");
  await loaded(page);
  const glossary = page.locator("details").filter({ has: page.locator("summary", { hasText: "Terms on this page" }) });
  await press(page, glossary.locator("summary"));
  await expect(glossary).toContainText("Projected gain");
  await expect(glossary).toContainText("never zero");
});

// ---------- Honest states ----------

test("league connections never shows fake connect controls when the prototype is off", async ({ page }) => {
  await page.goto("/leagues");
  await loaded(page);
  await expect(page.locator("main")).toContainText("switched off in this deployment");
  await expect(page.getByRole("button", { name: /Connect/ })).toHaveCount(0);
  await expect(page.locator("main")).toContainText("leagueId=");
});

test("start/sit opens with a verdict before the evidence", async ({ page }) => {
  await page.goto("/start-sit");
  await loaded(page);
  const verdict = page.getByRole("heading", { level: 2 }).first();
  await expect(verdict).toHaveText(/recommended move|optimal|preview|incomplete|No submitted lineup|kicked off|leaves/i);
});

// ---------- Touch targets and chrome ----------

test("phone chrome: tab bar and menu targets are at least 44px and the menu shows data freshness", async ({ page }) => {
  test.skip(!isPhone(), "phone chrome");
  await page.goto("/");
  await loaded(page);
  for (const link of await page.getByRole("navigation", { name: "Sections" }).getByRole("link").all()) {
    const box = await link.boundingBox();
    expect(box && box.height >= 44, "tab bar link shorter than 44px").toBe(true);
  }
  const toggle = page.getByRole("button", { name: /Open menu/ });
  const t = await toggle.boundingBox();
  expect(t && t.height >= 44 && t.width >= 44, "menu button smaller than 44px").toBe(true);
  await press(page, toggle);
  await expect(page.getByRole("navigation", { name: "All pages" }).getByRole("link", { name: /^Data ·/ })).toBeVisible();
});

test("phone fields use 16px text so iOS does not zoom on focus", async ({ page }) => {
  test.skip(!isPhone(), "phone fields");
  await page.goto("/rankings");
  await loaded(page);
  const size = await page.locator("#rankings-q").evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  expect(size).toBeGreaterThanOrEqual(16);
});


test("unscoped waiver shortlist cannot save into a shared fallback", async ({ page }) => {
  await page.goto("/waivers?view=ALL");
  const section = page.getByRole("region", { name: "Waiver shortlist" });
  await expect(section).toContainText("Open a league to save");
  const buttons = page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ });
  expect(await buttons.count()).toBeGreaterThan(0);
  for (const button of await buttons.all()) await expect(button).toBeDisabled();
  expect(await page.evaluate(() => Object.keys(localStorage).filter((key) => key.startsWith("fi:waiver-shortlist:")))).toEqual([]);
});
