import { expect, test, type Page } from "@playwright/test";

// Availability timeline on /injury-impact/timeline (issue #176) against the synthetic league (scripts/build_e2e_db.py).
// The fixture has no NFL injury report at all, so the page must say so in words: every player without a provider
// designation is unknown (never healthy), practice participation is unknown everywhere, the missing daily practice
// data is a visible notice, and every item names its source. Superseding reports, time zones, conflicts and
// roster filtering are covered by backend/tests/test_availability_timeline.py and lib/availability-timeline.test.ts.
// Runs on desktop-1440 and mobile-375.

// loading.tsx streams the same h1 first, so "loaded" is the digest's own content, never the heading or a skeleton.
const digest = (page: Page) => page.getByTestId("availability-digest");

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText("Availability", { timeout: 20_000 });
  await expect(digest(page), "digest still loading").toBeVisible({ timeout: 30_000 });
}

test("without an NFL report every player is unknown, never healthy, and every item cites its source", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/injury-impact/timeline");
  await loaded(page);
  const main = page.locator("main");
  await expect(main.getByText("The NFL injury report has never been synced", { exact: false })).toBeVisible();
  await expect(main.getByText(/Daily DNP \/ LP \/ FP progression is not in any integrated feed/).first()).toBeVisible();

  const cards = digest(page).locator("article[data-player]");
  expect(await cards.count()).toBeGreaterThan(0);
  const unknown = digest(page).locator('article[data-level="unknown"]');
  expect(await unknown.count()).toBeGreaterThan(0);
  await expect(unknown.first()).toContainText("The NFL injury report has never been synced: availability unknown.");
  // Practice is unknown for every player: nothing is read as full practice.
  const practice = await digest(page).locator("article[data-player] dd").allInnerTexts();
  expect(practice.join(" ")).not.toMatch(/\(FP\)|\(LP\)|\(DNP\)/);
  const text = (await digest(page).innerText()).toLowerCase();
  for (const word of ["healthy.", "cleared to play", "probability", "fantasypros"]) expect(text, word).not.toContain(word);

  // Each card links to the profile, injury impact and the lineup advice instead of advising.
  const first = cards.first();
  await expect(first.getByRole("link", { name: "Player page" })).toHaveAttribute("href", /\/players\?id=\d+/);
  await expect(first.getByRole("link", { name: "Lineup advice (Start/Sit)" })).toHaveAttribute("href", /\/start-sit/);
  await expect(first.getByRole("link", { name: "Injury impact" })).toHaveAttribute("href", /\/injury-impact/);

  // A provider status in the timeline names its source and says the provider publishes no time.
  const designated = digest(page).locator('article[data-player]:not([data-level="unknown"])');
  if (await designated.count()) {
    const card = designated.first();
    await card.locator("details summary").click();
    const ev = card.locator('li[data-kind="provider_status"]').first();
    await expect(ev).toContainText("ESPN player status");
    await expect(ev).toContainText(/publishes no time/);
  }

  // The sources inventory: official datasets link out; nothing is scraped or paid for.
  const sources = main.getByRole("region", { name: "Sources" });
  await expect(sources.getByText("NFL injury report (via nflverse)")).toBeVisible();
  await expect(sources.getByRole("link", { name: "dataset" }).first()).toHaveAttribute("href", /^https:\/\/github\.com\/nflverse\//);

  const [scroll, width] = await page.evaluate(() => [document.scrollingElement!.scrollWidth, window.innerWidth]);
  expect(scroll, "no horizontal page scroll").toBeLessThanOrEqual(width);
  expect(errors.filter((e) => !/espncdn|a\.espncdn|Failed to load resource/.test(e))).toEqual([]);
});

test("the digest follows the analyzed team and keeps it in its links", async ({ page }) => {
  await page.goto("/injury-impact/timeline?team=3");
  await loaded(page);
  await expect(page.locator("main")).not.toContainText("could not be loaded");
  const link = digest(page).locator("article[data-player]").first().getByRole("link", { name: "Lineup advice (Start/Sit)" });
  await expect(link).toHaveAttribute("href", /team=3/);
});

test("an unknown team is refused, never another team's roster", async ({ page }) => {
  await page.goto("/injury-impact/timeline?team=999");
  await expect(page.locator("main h1:visible")).toHaveText("Availability", { timeout: 20_000 });
  await expect(digest(page)).toHaveCount(0);
});
