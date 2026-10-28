import { expect, test, type Page } from "@playwright/test";
import { open, stubPhotos } from "./ux";

// The name-free clipboard exports (#197) and the Waivers stash planner (#218) against the synthetic fixture league
// (playwright.config.ts). The fixture's team and player names stand in for private identities: none may reach a copy.
// The stash checks read the plan straight from the fixture backend (playwright.config.ts API_PORT) and hold the panel
// to it, so the panel is tested on what the endpoint serves rather than on a hand-made payload.
const API = "http://127.0.0.1:8765";

test.beforeEach(async ({ page, context, baseURL }) => {
  await stubPhotos(page);
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL! });
});

const copyStatus = (page: Page) => page.locator("main [role=status]").filter({ hasText: /copied|Clipboard unavailable/ });

/** Every team and player name the page itself shows: what a name-free copy must leave out. */
async function namesOnPage(page: Page): Promise<string[]> {
  const names = await page.locator("main").locator('a[href*="/players?id="], a[href*="/managers/"]').allInnerTexts();
  return [...new Set(names.map((n) => n.trim()).filter((n) => n.length >= 3))];
}

const EXPORTS = [
  { path: "/matchup", copy: "Copy name-free summary" },
  { path: "/matchup?week=2", copy: "Copy name-free summary" },
  { path: "/weekly-recap", copy: "Copy name-free summary" },
  { path: "/my-team/value", copy: "Copy name-free roster summary" },
  { path: "/trades?ta=2&tb=1&a=206&a=307&b=202", copy: "Copy name-free trade summary" },
];

for (const { path, copy } of EXPORTS) {
  test(`${path}: the copied summary carries none of the names, ids or links on the page`, async ({ page }) => {
    await open(page, path);
    const names = await namesOnPage(page);
    expect(names.length, "the fixture page shows team or player names to compare against").toBeGreaterThan(0);
    await page.getByRole("button", { name: copy, exact: true }).click();
    await expect(copyStatus(page)).toContainText("copied");
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    for (const name of names) expect(copied, `copied text must not contain "${name}"`).not.toContain(name);
    expect(copied).not.toMatch(/https?:\/\/|\/l\/lg_|league_id|team_id|espn_player_id|\bnull\b|\bundefined\b|\bNaN\b|\[object/);
  });
}

test("a copy confirmation describes only the summary it copied, not the next week's", async ({ page }) => {
  await open(page, "/matchup");
  await page.getByRole("button", { name: "Copy name-free summary", exact: true }).click();
  await expect(copyStatus(page)).toHaveText("Name-free summary copied.");
  const week3 = await page.evaluate(() => navigator.clipboard.readText());
  // a soft navigation keeps the export control mounted: its old confirmation must not claim week 2 was copied
  await page.getByRole("link", { name: "← Week 2" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Week 2 matchup");
  await expect(copyStatus(page)).toHaveCount(0);
  await page.getByRole("button", { name: "Copy name-free summary", exact: true }).click();
  await expect(copyStatus(page)).toHaveText("Name-free summary copied.");
  const week2 = await page.evaluate(() => navigator.clipboard.readText());
  expect(week2).toContain("Week 2");
  expect(week2).not.toEqual(week3);
});

test("a copy confirmation does not survive a Team Value phase switch", async ({ page }) => {
  await open(page, "/my-team/value");
  await page.getByRole("button", { name: "Copy name-free roster summary", exact: true }).click();
  await expect(copyStatus(page)).toHaveText("Name-free roster summary copied.");
  await page.getByRole("link", { name: "Playoffs", exact: true }).click();
  await expect(page).toHaveURL(/phase=playoff/);
  await expect(copyStatus(page)).toHaveCount(0);
});

test("a refused clipboard says so and never reports a copy", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true, value: { writeText: () => Promise.reject(new DOMException("denied", "NotAllowedError")) },
    });
  });
  await open(page, "/matchup");
  await page.getByRole("button", { name: "Copy name-free summary", exact: true }).click();
  await expect(copyStatus(page)).toHaveText("Clipboard unavailable. Print this page instead.");
});

type Plan = { horizon: number; later_weeks: number[]; team: { name: string };
  counts: Record<"considered" | "evaluated" | "excluded_unprojected" | "excluded_position_limit" | "no_possible_gain"
    | "not_evaluated_cap", number> };

const stashPanel = (page: Page) => page.locator("section").filter({ has: page.getByRole("heading", { name: "Future-week stashes" }) });

for (const { team, horizon } of [{ team: 2, horizon: 4 }, { team: 1, horizon: 6 }, { team: 1, horizon: 2 }]) {
  test(`stash panel for team ${team} over ${horizon} weeks shows the served window and accounts for every candidate`, async ({ page, request }) => {
    const res = await request.get(`${API}/api/my-team/stash?team_id=${team}&horizon=${horizon}`);
    expect(res.ok()).toBe(true);
    const plan = (await res.json()) as Plan;
    await open(page, `/waivers?team=${team}&stashHorizon=${horizon}`);
    const panel = stashPanel(page);
    await expect(panel.getByRole("link", { name: `${horizon} weeks` })).toHaveAttribute("aria-current", "page");
    const later = plan.later_weeks;
    await expect(panel).toContainText(`For ${plan.team.name} · ${later.length} later weeks (weeks ${later[0]}–${later.at(-1)}`);
    const c = plan.counts;
    await expect(panel).toContainText(`Compared ${c.considered} eligible free agents`);
    // each nonzero part of the backend's split of `considered` is named, so the evaluated list is not the whole pool
    for (const [n, text] of [
      [c.evaluated, "evaluated exactly"], [c.excluded_unprojected, "excluded for a missing projection"],
      [c.excluded_position_limit, "excluded by a roster position limit"], [c.no_possible_gain, "with no possible gain"],
      [c.not_evaluated_cap, "beyond the per-position evaluation cap"],
    ] as const) {
      if (n) await expect(panel).toContainText(`${n} ${text}`);
    }
  });
}

test("switching the stash horizon shows the newly requested window, not the previous one", async ({ page }) => {
  await open(page, "/waivers?stashHorizon=2");
  const panel = stashPanel(page);
  await expect(panel).toContainText("2 later weeks (weeks 4–5)");
  await panel.getByRole("link", { name: "6 weeks" }).click();
  await expect(page).toHaveURL(/stashHorizon=6/);
  await expect(panel.getByRole("link", { name: "6 weeks" })).toHaveAttribute("aria-current", "page");
  await expect(panel).toContainText("6 later weeks (weeks 4–9)");
  await expect(panel).not.toContainText("2 later weeks");
});
