import { expect, test } from "@playwright/test";
import {
  applyTheme, collectErrors, expectAccessible, expectAppBarFits, expectDecisionFirst, expectNoBrokenValues,
  expectNoSidewaysScroll, expectStructure, open, stubPhotos, tabThrough, useTheme,
} from "./ux";
import { RAIL_KEY } from "../lib/navigation";

// UX acceptance gates for the core destinations (issue #202) on the demo/public ESPN fixture league
// (scripts/build_e2e_db.py: team 2 is the owner default, team 1 a rival, 201 "Echo RB", 206/307 on team 2, 202 on
// team 1). Runs on the 320, 390, 768 and 1440 projects (playwright.config.ts). Each route, at each width, in both
// themes: one h1 and the landmarks, decision before evidence, no sideways scroll, an app bar that fits, no leaked
// undefined/NaN or engine identifiers, axe-core clean (or triaged in e2e/ux-triage.ts) and zero console errors.
// The private ESPN, Sleeper, stale-sync and blank-sync fixtures are covered by e2e/ux-multileague.spec.ts.
type Core = { name: string; path: string; verdict?: boolean; aboveFold?: boolean };
const CORE: Core[] = [
  { name: "Overview", path: "/" },
  { name: "Start/Sit", path: "/start-sit", verdict: true, aboveFold: true },
  { name: "Matchup", path: "/matchup" },
  { name: "Team value", path: "/my-team/value" },
  { name: "Waivers", path: "/waivers", verdict: true, aboveFold: true },
  { name: "Trade analyzer", path: "/trades" },
  { name: "Trade result", path: "/trades?ta=2&tb=1&a=206&a=307&b=202", verdict: true },
  { name: "Rankings", path: "/rankings" },
  { name: "Player profile", path: "/players?id=201" },
  { name: "Weekly recap", path: "/weekly-recap" },
  { name: "Transactions", path: "/transactions?type=trade" },
  { name: "Playoff race", path: "/playoff-race" },
  { name: "Manager", path: "/managers/1" },
  { name: "League analyzer", path: "/league-analyzer?vs=1", verdict: true },
  // The league connection page; with MULTILEAGUE_UI_ENABLED unset (as here) it explains the single-league setup.
  { name: "Leagues", path: "/leagues" },
  { name: "Screener", path: "/screener" },
];

// Colours fade for 120ms on a theme switch; reduced motion makes the switch instant, so axe never samples mid-fade.
test.use({ reducedMotion: "reduce" });

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
});

for (const route of CORE) {
  test(`${route.name} meets the quality bar in both themes`, async ({ page }) => {
    const errors = collectErrors(page);
    await useTheme(page, "dark");
    await open(page, route.path);
    if (route.verdict) {
      // The trade result streams in after the builder.
      await expect(page.locator('main [class*="verdict"]').first(), "Verdict rendered").toBeVisible({ timeout: 30_000 });
    }
    await expectStructure(page);
    await expectNoBrokenValues(page);
    await expectDecisionFirst(page, { verdict: !!route.verdict, aboveFold: !!route.aboveFold && (page.viewportSize()?.width ?? 0) >= 1024 });
    await expectAppBarFits(page);
    // axe in both themes at 390 and 1440; at 320 and 768 the light pass checks layout only, since the theme changes
    // colours, not geometry (keeps the CI cost down; contrast for every token pair is also unit-tested).
    const bothThemes = ["mobile-390", "desktop-1440"].includes(test.info().project.name);
    for (const theme of ["dark", "light"] as const) {
      await test.step(theme, async () => {
        await applyTheme(page, theme);
        await expectNoSidewaysScroll(page);
        if (theme === "dark" || bothThemes) await expectAccessible(page, theme);
      });
    }
    expect(errors, "console errors").toEqual([]);
  });
}

test("trade builder checkboxes expose 24px targets while keeping 18px indicators", async ({ page }) => {
  await open(page, "/trades");
  const targets = await page.getByRole("checkbox").evaluateAll((boxes) => boxes.map((box) => {
    const rect = box.getBoundingClientRect();
    return { width: rect.width, height: rect.height, indicator: getComputedStyle(box, "::before").width };
  }));
  expect(targets.length, "trade builder has selectable players").toBeGreaterThan(0);
  for (const target of targets) {
    expect(target.width, "checkbox hit target width").toBeGreaterThanOrEqual(24);
    expect(target.height, "checkbox hit target height").toBeGreaterThanOrEqual(24);
    expect(target.indicator, "visual checkbox width").toBe("18px");
  }
});

// The CORE runs above see the wide desktop with the workspace rail open (its default); this is the other state.
test("the collapsed workspace rail meets the quality bar on a wide desktop", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) < 1200, "the rail is the wide desktop layout");
  const errors = collectErrors(page);
  await page.addInitScript((key) => {
    try { window.localStorage.setItem(key, "collapsed"); } catch { /* storage blocked: the rail stays open */ }
  }, RAIL_KEY);
  for (const path of ["/", "/waivers", "/movers"]) {
    await open(page, path);
    await test.step(path, async () => {
      await expect(page.locator("html")).toHaveAttribute("data-rail", "collapsed");
      await expectStructure(page);
      await expectAppBarFits(page);
      await expectNoSidewaysScroll(page);
      await expectAccessible(page, "rail collapsed");
    });
  }
  expect(errors, "console errors").toEqual([]);
});

test("keyboard: every focus stop is visible and shows a focus ring, from the app bar into the page", async ({ page }) => {
  for (const path of ["/", "/start-sit", "/waivers", "/rankings"]) {
    await open(page, path);
    const stops = await tabThrough(page, 80);
    await test.step(path, () => {
      expect(stops.length, `${path}: Tab moves focus`).toBeGreaterThan(0);
      expect(stops[0].where, `${path}: the first stop is in the app bar`).toBe("banner");
      expect(stops.filter((s) => !s.visible).map((s) => s.name), `${path}: focus on hidden elements`).toEqual([]);
      expect(stops.filter((s) => !s.indicator).map((s) => `${s.where}: ${s.name}`), `${path}: focus stops without a visible indicator`).toEqual([]);
      expect(stops.some((s) => s.where === "main"), `${path}: Tab reaches the page content within 80 presses`).toBe(true);
    });
  }
});

test("keyboard: a table that scrolls sideways is a tab stop and scrolls with the arrow keys; one that fits is not", async ({ page }) => {
  for (const path of ["/weekly-recap", "/playoff-race", "/"]) {
    await open(page, path);
    await test.step(path, async () => {
      // components/TableScroll.tsx marks overflowing tables once per frame after the DOM settles.
      await expect.poll(() => page.locator("main .table-wrap").evaluateAll((els) => els.every((e) =>
        (e.scrollWidth > e.clientWidth + 1) === (e.getAttribute("tabindex") === "0"))), {
        message: "tabindex=0 on exactly the tables that overflow",
      }).toBe(true);
      const scroller = page.locator("main .table-wrap[tabindex='0']").first();
      if (await scroller.count() === 0) return;           // everything fits at this width
      await scroller.focus();
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => scroller.evaluate((e) => e.scrollLeft), { message: "arrow keys scroll the focused table" }).toBeGreaterThan(0);
    });
  }
});

// The not-found page (app/not-found.tsx) answers 404 with the app shell: the same quality bar as a core destination,
// at every width and in both themes. Its only console line is Chromium's own report of the 404 document.
test("Not found meets the quality bar in both themes", async ({ page }) => {
  const errors = collectErrors(page, { documentMayBe404: true });
  await useTheme(page, "dark");
  await open(page, "/no-such-page", 404);
  await expect(page.locator("main h1")).toHaveText("Page not found");
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectAppBarFits(page);
  const bothThemes = ["mobile-390", "desktop-1440"].includes(test.info().project.name);
  for (const theme of ["dark", "light"] as const) {
    await test.step(theme, async () => {
      await applyTheme(page, theme);
      await expectNoSidewaysScroll(page);
      if (theme === "dark" || bothThemes) await expectAccessible(page, theme);
    });
  }
  expect(errors, "console errors").toEqual([]);
});

// The Overview's weekly decision hub (issue #165) on the demo league, at every width in light (the CORE run above
// checks the light theme's layout only at 320 and 768): the next step is a decision with its evidence and a link that
// keeps the week, and the other decisions sit under "Before kickoff". Axe-clean in light at this width.
test("the Overview's weekly hub shows one next step and the decisions behind it (light)", async ({ page }) => {
  const errors = collectErrors(page);
  await useTheme(page, "light");
  await open(page, "/");
  const main = page.locator("main");
  const eyebrow = /^Next step · Week \d+ · \w+/;
  await expect(main.getByText(eyebrow)).toBeVisible();
  // The next step's own evidence: the provenance badge (or the explicit unknown) inside the same verdict.
  const next = main.locator('[class*="verdict"]').filter({ has: page.getByText(eyebrow) });
  await expect(next).toHaveCount(1);
  await expect(next.getByText(/Evidence age unknown|(Current data|Partial data|Stale data|Data age unknown|Data unavailable) · /).first()).toBeVisible();
  await expect(main.getByRole("heading", { name: "Before kickoff" })).toBeVisible();
  await expect(main.getByText("Lineup is set")).toHaveCount(0);
  await expectNoBrokenValues(page);
  await expectNoSidewaysScroll(page);
  await expectAccessible(page, "light");
  expect(errors, "console errors").toEqual([]);
});
