import fs from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  applyTheme, collectErrors, expectAccessible, expectAppBarFits, expectDecisionFirst, expectNoBrokenValues,
  expectNoSidewaysScroll, expectStructure, stubPhotos, tabThrough, useTheme,
} from "./ux";

// League compatibility journey (issue #253), on the multi-league site (playwright.multileague.config.ts): the demo and
// the connect page, then the same decisions in a supported league and in an unsupported one. Each step opens the league
// rules, Start/Sit, the add/drop simulator on Waivers and an evaluated trade, at 1440 and 375px, in both themes (axe),
// with a keyboard pass. Data (scripts/e2e_multileague.py, all invented):
//   * espn_public: the ESPN fixture league (scripts/build_e2e_db.py): every starting slot is filled by the engines;
//   * sleeper_idp: the Sleeper fixture league plus a DL and an LB starting slot (scripts/build_e2e_sleeper.py --idp).
//     No engine fills those slots and the sync stores no IDP player, so the rules page must say so, lineup totals must
//     be marked partial, roster legality unverified, and nothing may show a measured 0 for what is missing.
// Team 1 is the perspective in both leagues; 209 "Waiver RB" is a free agent in both; 202 (team 1) for 204 (team 2).
type Manifest = { leagues: Record<"espn_public" | "sleeper_idp", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));

const ADD = "/waivers?team=1&add=209";
const TRADE = "/trades?ta=1&tb=2&a=202&b=204";

/** Navigate and wait for the page's own heading (never the loading.tsx skeleton, which has no h1 text). */
async function visit(page: Page, url: string, h1: string | RegExp) {
  const res = await page.goto(url, { waitUntil: "load" });
  expect(res?.status(), `${url}: HTTP status`).toBe(200);
  await expect(page.locator("main h1"), `${url}: page heading`).toHaveText(h1, { timeout: 30_000 });
  await expect(page.locator('main [aria-busy="true"]'), `${url}: nothing still loading`).toHaveCount(0, { timeout: 30_000 });
}

/**
 * The PRODUCT.md quality bar on the page as it is now, axe in both themes. From the top of the page: an evaluated trade
 * scrolls itself to the result, and axe's target-size rule counts a control the sticky app bar covers at that scroll
 * position as too small.
 */
async function qualityBar(page: Page, verdict: boolean) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectDecisionFirst(page, { verdict });
  await expectAppBarFits(page);
  for (const theme of ["dark", "light"] as const) {
    await applyTheme(page, theme);
    await expectNoSidewaysScroll(page);
    await expectAccessible(page, theme);
  }
}

/** From the top of the page, Tab reaches the content; every stop is visible and shows a focus ring. */
async function keyboard(page: Page) {
  const stops = await tabThrough(page, 80);
  expect(stops.length, "Tab moves focus").toBeGreaterThan(0);
  expect(stops[0].where, "the first stop is in the app bar").toBe("banner");
  expect(stops.filter((s) => !s.visible).map((s) => s.name), "focus on hidden elements").toEqual([]);
  expect(stops.filter((s) => !s.indicator).map((s) => `${s.where}: ${s.name}`), "focus stops without a ring").toEqual([]);
  expect(stops.some((s) => s.where === "main"), "Tab reaches the page content").toBe(true);
}

/** A Verdict figure's value (`<dt>label</dt><dd>value</dd>`). */
const figure = (page: Page, label: string): Locator =>
  page.locator("main dt", { hasText: new RegExp(`^${label.replace(/[()]/g, "\\$&")}$`, "i") }).first().locator("xpath=following-sibling::dd[1]");
const slotRow = (page: Page, code: string): Locator =>
  page.locator("#roster tbody tr").filter({ has: page.locator("td:first-child b", { hasText: new RegExp(`^${code}$`) }) });

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
  await useTheme(page, "dark");
});

for (const width of [1440, 375]) {
  test.describe(`compatibility journey at ${width}px`, () => {
    test.use({ viewport: { width, height: width < 720 ? 812 : 900 } });

    test(`demo → connect → supported league → unsupported league: rules, lineup, waivers, trades (${width}px)`, async ({ page }) => {
      test.setTimeout(240_000);
      const errors = collectErrors(page);
      // Read-only throughout: nothing reaches a provider, and the browser sends nothing but GET to an API route.
      const writes: string[] = [];
      page.on("request", (r) => {
        const url = new URL(r.url());
        if (/(^|\.)(espn\.com|sleeper\.(app|com))$/.test(url.hostname)) writes.push(`provider: ${r.method()} ${r.url()}`);
        if (url.pathname.includes("/api/") && r.method() !== "GET") writes.push(`${r.method()} ${url.pathname}`);
      });
      const { espn_public: espn, sleeper_idp: idp } = manifest().leagues;

      await test.step("demo league, then the connect page", async () => {
        await visit(page, "/leagues/demo", "Fixture League");
        await expect(page.locator("main")).toContainText("Everything on this page is invented");
        await expect(figure(page, "Best lineup")).toHaveText(/^\d+\.\d$/);
        await qualityBar(page, true);
        await page.getByRole("link", { name: "Connect your league" }).click();
        await expect(page).toHaveURL(/\/leagues$/);
        await expect(page.locator("main h1")).toHaveCount(1);
      });

      await test.step("supported: every slot is filled by the engines", async () => {
        await visit(page, `/l/${espn}/league-rules`, "League rules");
        await expect(page.locator("#roster tbody tr")).toHaveCount(9);
        for (const code of ["QB", "RB", "WR", "TE", "FLEX", "K", "D/ST"]) {
          await expect(slotRow(page, code), `${code} slot`).toContainText("Handled");
        }
        await expect(page.locator("#roster")).not.toContainText("Not applied");
        await expect(page.locator("main")).not.toContainText("Starting slots no engine fills");
        await qualityBar(page, false);
        await keyboard(page);
      });

      await test.step("supported: Start/Sit totals are complete", async () => {
        await visit(page, `/l/${espn}/start-sit?team=1`, /^Start\/Sit/);
        for (const label of ["Submitted (proj)", "Recommended (proj)"]) {
          await expect(figure(page, label), label).toHaveText(/^\d+\.\d$/);      // a measured total, no partial mark
        }
        await expect(page.locator("main")).not.toContainText("slots no engine fills");
        await qualityBar(page, true);
      });

      await test.step("supported: add/drop counts the active roster", async () => {
        await visit(page, `/l/${espn}${ADD}`, "Waivers");
        const sim = page.locator("#add-drop");
        await expect(figure(page, "Active roster")).toHaveText(/^\d+ of \d+$/, { timeout: 30_000 });
        await expect(sim).not.toContainText(/legality unverified/i);
        await qualityBar(page, true);
      });

      await test.step("supported: an evaluated trade checks roster legality", async () => {
        await visit(page, `/l/${espn}${TRADE}`, "Trade analyzer");
        await expect(page.locator("main")).toContainText("No drop is needed", { timeout: 30_000 });
        await expect(page.locator("main")).not.toContainText(/roster legality (is )?unverified/i);
        await qualityBar(page, true);
      });

      await test.step("unsupported: the rules page discloses the DL and LB slots", async () => {
        await visit(page, `/l/${idp}/league-rules`, "League rules");
        for (const code of ["DL", "LB"]) {
          const row = slotRow(page, code);
          await expect(row, `${code} slot`).toContainText("Not applied");     // league_rules "unsupported"
          await expect(row, `${code} slot`).toContainText("no engine fills this slot");
        }
        await expect(slotRow(page, "QB")).toContainText("Handled");
        await expect(page.getByRole("row", { name: /Starting slots no engine fills/ })).toContainText("DL, LB");
        await qualityBar(page, false);
        await keyboard(page);
      });

      await test.step("unsupported: Start/Sit totals are partial, never a measured 0", async () => {
        await visit(page, `/l/${idp}/start-sit?team=1`, /^Start\/Sit/);
        for (const label of ["Submitted (proj)", "Recommended (proj)"]) {
          const value = figure(page, label);
          await expect(value, `${label} is marked partial`).toHaveText(/^\d+\.\d\*$/);
          await expect(value, `${label} is not a measured 0`).not.toHaveText(/^0\.0/);
          await expect(value.locator("xpath=following-sibling::dd[1]"), `${label} note`)
            .toHaveText("*Partial: leaves out DL, LB (no engine fills these slots)");
        }
        // The two IDP starters are not in the player data: counted and named as missing, never shown with 0 points.
        await expect(page.locator("main")).toContainText("2 starters are not in the synced player data");
        await expect(page.locator("main").getByText(/^Sleeper (DL|LB) \d$/)).toHaveCount(0);
        await qualityBar(page, true);
        await keyboard(page);
      });

      await test.step("unsupported: add/drop says roster legality is unverified", async () => {
        await visit(page, `/l/${idp}${ADD}`, "Waivers");
        const sim = page.locator("#add-drop");
        await expect(figure(page, "Active roster")).toHaveText("Unverified", { timeout: 30_000 });
        await expect(sim).toContainText("Legality unverified");
        await expect(sim).toContainText("2 rostered players are not in the synced player data");
        await expect(sim).toContainText("Lineup totals leave DL, LB out, so each one is partial");
        // A value the waiver engine cannot measure is a titled dash, never 0.
        const missing = page.locator("main .missing[title]");
        expect(await missing.count(), "missing values render as a titled dash").toBeGreaterThan(0);
        await qualityBar(page, true);
      });

      await test.step("unsupported: an evaluated trade has partial totals and unverified legality", async () => {
        await visit(page, `/l/${idp}${TRADE}`, "Trade analyzer");
        const main = page.locator("main");
        await expect(main.getByText("Sleeper Alpha: roster legality unverified")).toBeVisible({ timeout: 30_000 });
        await expect(main.getByText("Sleeper Bravo: roster legality unverified")).toBeVisible();
        await expect(main).toContainText(/\d+\.\d → \d+\.\d pts\* \(partial\)/);
        await expect(main).not.toContainText("No drop is needed");
        await qualityBar(page, true);
      });

      expect(errors, "console errors").toEqual([]);
      expect(writes, "provider calls or API writes").toEqual([]);
    });
  });
}
