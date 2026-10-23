import { expect, test, type Page } from "@playwright/test";
import { collectErrors, expectAccessible, expectNoBrokenValues, expectNoSidewaysScroll, expectStructure, open, stubPhotos } from "./ux";

// Player screener (issue #226) against the synthetic research pool (backend/tests/synthetic_research.py: 16 WR, 12 RB
// and 10 TE over weeks 1-7, no route data; "Synthetic RB 1" is linked to the fixture league's player 201, rostered by
// Team 1). Presets and the builder live in the URL; every number is the backend's; missing route data makes rows
// undecided (never 0); ownership comes from the league. Runs on desktop-1440 and mobile-375.

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
});

// loading.tsx streams the same h1 and a skeleton table first, so "loaded" means the result table (its League column)
// or the empty state, after the builder.
const results = (page: Page) => page.getByRole("table").filter({ has: page.getByRole("columnheader", { name: "League" }) });
const rows = (page: Page) => results(page).locator("tbody tr");

async function loaded(page: Page, url: string) {
  await open(page, url);
  await expect(page.locator("main h1")).toHaveText("Player screener");
  await expect(page.getByRole("form", { name: "Screen builder" })).toBeAttached();
  await expect(results(page).or(page.getByText("No player matches this screen")).or(page.getByText(/This screen cannot run/)).first())
    .toBeVisible({ timeout: 30_000 });
}

test("the default screen lists the pool with samples, units, cutoffs and league ownership", async ({ page }) => {
  const errors = collectErrors(page);
  await loaded(page, "/screener");
  const main = page.locator("main");
  await expect(main.getByText(/2026 season · nflverse data through week 7 \(xFP through week 7\)/)).toBeVisible();
  await expect.poll(() => rows(page).count()).toBe(38);
  // at least five ready-made screens, each a link that opens an editable screen
  const presets = main.getByRole("region", { name: "Ready-made screens" }).getByRole("link");
  expect(await presets.count()).toBeGreaterThanOrEqual(5);
  // metric table: unit, denominator, source and the data cutoff
  const metrics = main.getByRole("region", { name: "Metrics in this screen" });
  await expect(metrics.getByRole("row", { name: /Expected points per game.*PPR points per game.*Week 7/ })).toBeVisible();
  // the RB linked to the league shows its owner and links to profile and Compare; unlinked players say so
  const rb1 = rows(page).filter({ has: page.getByRole("link", { name: "Synthetic RB 1", exact: true }) });
  await expect(rb1).toHaveCount(1);
  await expect(rb1.getByRole("link", { name: "Synthetic RB 1", exact: true })).toHaveAttribute("href", "/players?id=201");
  await expect(rb1.getByRole("link", { name: /^Compare Synthetic RB 1/ })).toHaveAttribute("href", /^\/compare\?a=201$/);
  await expect(rb1).toContainText("Team 1");
  const unknown = rows(page).filter({ has: page.getByText("Synthetic WR 1", { exact: true }) });
  await expect(unknown).toHaveCount(1);
  await expect(unknown).toContainText("Roster status unknown");
  await expect(unknown.getByRole("link", { name: /Add\/drop/ })).toHaveCount(0);       // unknown is never available
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectNoSidewaysScroll(page);
  await expectAccessible(page, "screener default");
  expect(errors).toEqual([]);
});

test("a preset runs an AND screen from the URL and sorting keeps it", async ({ page }) => {
  const errors = collectErrors(page);
  await loaded(page, "/screener");
  await page.getByRole("link", { name: "Volume without the points" }).click();
  await expect(page).toHaveURL(/fm=xfp_pg.*fm=fpoe_pg/);
  await expect(page.getByRole("link", { name: "Volume without the points" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("xFP/G ≥ 12 AND FPOE/G ≤ -2", { exact: true })).toBeVisible();
  await expect(rows(page)).toHaveCount(2);
  for (const t of await rows(page).locator("td:nth-child(4)").allInnerTexts()) {      // FPOE/G column: at most -2
    expect(Number.parseFloat(t)).toBeLessThanOrEqual(-2);
  }
  await results(page).getByRole("link", { name: /^xFP\/G/ }).click();
  await expect(page).toHaveURL(/sort=xfp_pg/);
  await expect(page).toHaveURL(/fm=fpoe_pg/);
  await expect(rows(page)).toHaveCount(2);
  const xfp = (await rows(page).locator("td:nth-child(3)").allInnerTexts()).map((t) => Number.parseFloat(t));
  expect([...xfp].sort((a, b) => b - a)).toEqual(xfp);
  await expectNoSidewaysScroll(page);
  expect(errors).toEqual([]);
});

test("missing route data makes players undecided, never zero", async ({ page }) => {
  await loaded(page, "/screener");
  await page.getByRole("link", { name: "WR routes and targets, last 3 games" }).click();
  await expect(page.getByText("No player matches this screen")).toBeVisible();
  await expect(page.getByText("No route data for these weeks")).toBeVisible();
  const undecided = page.getByRole("region", { name: "Could not be decided" });
  await expect(undecided).toBeVisible();
  await expect(undecided.getByText(/missing Route %/).first()).toBeVisible();
  await expect(page.locator("main")).not.toContainText(/0(\.0)?% route/i);
});

test("the builder composes an OR screen of two groups for one position", async ({ page }) => {
  const errors = collectErrors(page);
  await loaded(page, "/screener");
  const form = page.getByRole("form", { name: "Screen builder" });
  await form.getByRole("checkbox", { name: "RB" }).uncheck();
  await form.getByRole("checkbox", { name: "TE" }).uncheck();
  const groups = form.getByLabel("Group");
  const metrics = form.getByLabel("Metric");
  const values = form.getByLabel("Value");
  await metrics.nth(0).selectOption("target_share");
  await values.nth(0).fill("40");
  await groups.nth(1).selectOption({ label: "B" });
  await metrics.nth(1).selectOption("targets_pg");
  await values.nth(1).fill("10");
  await form.getByRole("button", { name: "Run screen" }).click();
  await expect(page).toHaveURL(/pos=WR(?!.*pos=RB)/);
  await expect(page).toHaveURL(/fm=target_share/);
  await expect(page.getByText("(Tgt % ≥ 40%) OR (Tgt/G ≥ 10)", { exact: true })).toBeVisible();
  await expect.poll(() => rows(page).count()).toBeGreaterThan(0);
  for (const name of await rows(page).locator("td:first-child").allInnerTexts()) expect(name).toMatch(/Synthetic WR/);
  // the screen survives a reload: the URL is the whole state
  await page.reload();
  await expect(page.getByText("(Tgt % ≥ 40%) OR (Tgt/G ≥ 10)", { exact: true })).toBeVisible();
  await expectNoSidewaysScroll(page);
  expect(errors).toEqual([]);
});

test("invalid and out-of-range screens are refused with the reason", async ({ page }) => {
  await loaded(page, "/screener?fm=xfp_pg&fv=abc");
  await expect(page.getByText("This screen cannot run yet")).toBeVisible();
  await expect(results(page)).toHaveCount(0);
  await loaded(page, "/screener?to=10");
  await expect(page.getByText("This screen cannot run", { exact: true })).toBeVisible();
  await expect(page.getByText(/after the latest week with data \(week 7\)/)).toBeVisible();
  await loaded(page, "/screener?fm=backfield_share&fv=50&pos=WR");
  await expect(page.getByText(/not defined for WR/)).toBeVisible();
});
