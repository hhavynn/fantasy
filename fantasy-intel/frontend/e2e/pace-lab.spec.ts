import { expect, test, type Page } from "@playwright/test";
import { applyTheme, collectErrors, expectAccessible, expectNoBrokenValues, expectNoSidewaysScroll, expectStructure, stubPhotos } from "./ux";

// Pace and game-script lab (issue #211) against the synthetic seasons in scripts/build_e2e_db.py
// (backend/tests/synthetic_pace.py): 32 offenses in 2024 and 2025, one overtime game per season, KC's week-7 game with
// no stored play-by-play, and LV listed as OAK in 2024. Observed values and model assumptions sit in separate,
// labelled columns; the script mix is hypothetical; the frozen backtest's verdicts are worded as the result says.
// Runs on desktop-1440 and mobile-375.

test.beforeEach(async ({ page }) => { await stubPhotos(page); });

// loading.tsx streams the same h1 and a skeleton table first, so "loaded" is a real table with its own headers.
const table = (page: Page, header: RegExp) =>
  page.locator("main table").filter({ has: page.getByRole("columnheader", { name: header }) }).first();

async function loaded(page: Page, url: string, header: RegExp) {
  const res = await page.goto(url, { waitUntil: "load" });
  expect(res?.status(), "HTTP status").toBe(200);
  await expect(page.locator("main h1:visible")).toHaveText("Pace lab", { timeout: 20_000 });
  await expect(table(page, header), "table still streaming").toBeVisible({ timeout: 30_000 });
  await expect(page.locator("main [aria-busy='true']")).toHaveCount(0);
}

async function openFilters(page: Page) {
  const box = page.locator("main details.filter-box");
  if ((await box.getAttribute("open")) === null) await box.locator("summary").click();
}

test("every offense: observed beside assumed, the backtest worded as it came out", async ({ page }) => {
  const errors = collectErrors(page);
  await loaded(page, "/pace-lab", /Assumed plays\/g/);
  const main = page.locator("main");
  const overview = table(page, /Assumed plays\/g/);
  await expect(overview.locator("tbody tr")).toHaveCount(32);
  await expect(overview.getByRole("columnheader", { name: /Observed plays\/g/ })).toBeVisible();
  await expect(overview.getByRole("link", { name: "OAK", exact: true })).toHaveCount(0);
  await expect(main.getByText("Observed and assumed are kept apart")).toBeVisible();
  await expect(main.getByText(/did not forecast the next game better than the league mean to date/).first()).toBeVisible();
  await expect(main.getByText(/the shrunk neutral pass rate forecast the next game better than both simple baselines/).first()).toBeVisible();
  const backtest = table(page, /^MAE$/);
  await expect(backtest.getByText("No clear difference from the better simple baseline")).toBeVisible();
  await expect(main.getByText(/1 final team-game in this filter has no stored play-by-play aggregate/)).toBeVisible();
  await expect(main.getByText("What this lab cannot measure")).toBeVisible();
  await expect(main.getByText(/Seconds per play: the game clock at each snap is not stored/)).toBeVisible();
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectNoSidewaysScroll(page);
  await expectAccessible(page, "light");
  await applyTheme(page, "dark");
  await expectAccessible(page, "dark");
  await expectNoSidewaysScroll(page);
  expect(errors).toEqual([]);
});

test("a team-week view with a hypothetical script mix", async ({ page }) => {
  const errors = collectErrors(page);
  await loaded(page, "/pace-lab", /Assumed plays\/g/);
  await table(page, /Assumed plays\/g/).getByRole("link", { name: "SEA", exact: true }).click();
  await expect(page).toHaveURL(/team=SEA/);
  await expect(table(page, /Neutral pass/), "team-week log").toBeVisible({ timeout: 30_000 });
  const main = page.locator("main");
  await expect(table(page, /Neutral pass/).locator("tbody tr")).toHaveCount(17);
  await expect(table(page, /Assumption/).getByRole("rowheader", { name: "Plays per game" })).toBeVisible();
  await expect(main.getByText("Hypothetical, not a forecast")).toBeVisible();
  // The observed mix is the starting point: the scenario equals it, so every change in centre is +0.0 or 0.0.
  const scenario = table(page, /Change in centre/);
  await expect(scenario.locator("tbody tr")).toHaveCount(4);
  for (const t of await scenario.locator("tbody tr td:last-child").allInnerTexts()) expect(t.trim()).toMatch(/^0\.0$/);
  // A preset, then a typed mix: the URL carries it and the plays centre moves.
  await main.getByRole("link", { name: "Mostly trailing" }).click();
  await expect(page).toHaveURL(/mc=20&ml=10&mt=70/);
  await expect(main.getByText(/Scenario: 20% competitive, 10% leading, 70% trailing/)).toBeVisible({ timeout: 30_000 });
  await main.getByLabel("Competitive %").fill("0");
  await main.getByLabel("Leading %").fill("100");
  await main.getByLabel("Trailing %").fill("0");
  await main.getByRole("button", { name: "Apply mix" }).click();
  await expect(page).toHaveURL(/mc=0&ml=100&mt=0/);
  await expect(main.getByText(/Scenario: 0% competitive, 100% leading, 0% trailing/)).toBeVisible({ timeout: 30_000 });
  const plays = table(page, /Change in centre/).getByRole("row", { name: /^Plays/ });
  await expect(plays.locator("td").last()).toHaveText(/^-\d+\.\d$/);
  await expect(main.getByText(/middle 80% of past team-games shaped like the mix/)).toBeVisible();
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectNoSidewaysScroll(page);
  await expectAccessible(page, "team view");
  expect(errors).toEqual([]);
});

test("pooled seasons merge a relocated franchise, missing games stay missing", async ({ page }) => {
  await loaded(page, "/pace-lab?seasons=2&team=LV", /Neutral pass/);
  const main = page.locator("main");
  await expect(main.getByText(/Franchise codes merged across seasons: OAK → LV/)).toBeVisible();
  const log = table(page, /Neutral pass/);
  await expect(log.locator("tbody tr")).toHaveCount(34);
  await expect(log.getByText("listed as OAK").first()).toBeVisible();
  await loaded(page, "/pace-lab?team=KC", /Neutral pass/);
  const kc = table(page, /Neutral pass/);
  const w7 = kc.getByRole("row", { name: /^W7\b/ });
  await expect(w7.locator("td").nth(3)).toHaveText("—");
  await expect(w7.locator("td").nth(3).locator(".missing")).toHaveAttribute("title", /No play-by-play aggregate is stored/);
  // The opponent filter keeps only head-to-head games and adds the labelled matchup assumption.
  await openFilters(page);
  const opp = await kc.locator("tbody tr").first().locator("td").first().innerText();
  await main.getByLabel("Opponent (with a team)").selectOption(opp.replace("@ ", "").trim());
  await main.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(main.getByText(/Matchup assumption: KC against/)).toBeVisible({ timeout: 30_000 });
  await expect(main.getByText(/the additive opponent adjustment forecast the next game worse than the league mean to date/)).toBeVisible();
  await expectNoSidewaysScroll(page);
});
