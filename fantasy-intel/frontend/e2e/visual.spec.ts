import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { open, stubPhotos, useTheme, type Theme } from "./ux";

// Visual regression (issue #202), run by playwright.visual.config.ts: the first screenful of six core destinations
// and the player chart, on a phone, a tablet and a desktop, in light and dark, against reviewed baselines in
// e2e/visual-baselines/. The set is deliberately small; layout, overflow, landmarks and accessibility are asserted
// structurally by e2e/ux-gates.spec.ts, which does not depend on pixels.
//
// Baselines are only valid for the browser build that drew them, recorded in visual-baselines/BROWSER.txt:
//   - none committed yet: every test skips and says so (the gate is not armed);
//   - recorded on another browser: CI fails (regenerate and review), a local run skips (compare in CI);
//   - --update-snapshots=all (the "Visual baselines" workflow): writes new baselines and the browser stamp.
// See docs/qa/release-review.md: baselines are reviewed, never blindly regenerated.
const BASELINES = path.resolve(__dirname, "visual-baselines");
const STAMP = path.join(BASELINES, "BROWSER.txt");

const ROUTES = [
  { name: "overview", path: "/" },
  { name: "start-sit", path: "/start-sit" },
  { name: "waivers", path: "/waivers" },
  { name: "rankings", path: "/rankings" },
  { name: "player", path: "/players?id=201" },
  { name: "trade-result", path: "/trades?ta=2&tb=1&a=206&a=307&b=202" },
];

/** Themes per project: both on the phone and the desktop, light on the tablet. */
const themesFor = (project: string): Theme[] => (project.includes("tablet") ? ["light"] : ["dark", "light"]);

test.beforeEach(async ({ page, browser }, info) => {
  await stubPhotos(page);
  const running = `${browser.browserType().name()} ${browser.version()}`;
  if (info.config.updateSnapshots === "all" || info.config.updateSnapshots === "changed") {
    fs.mkdirSync(BASELINES, { recursive: true });
    fs.writeFileSync(STAMP, `${running}\n`);
    return;
  }
  if (!fs.existsSync(STAMP)) {
    test.skip(true, "No reviewed visual baselines are committed yet: run the Visual baselines workflow (docs/qa/release-review.md).");
    return;
  }
  const recorded = fs.readFileSync(STAMP, "utf8").trim();
  if (recorded !== running) {
    if (process.env.CI) {
      throw new Error(`Visual baselines were drawn by ${recorded}; this run uses ${running}. Regenerate them with the `
        + `Visual baselines workflow and review every image before committing (docs/qa/release-review.md).`);
    }
    test.skip(true, `Baselines were drawn by ${recorded}; this browser is ${running}. Compare in CI.`);
  }
});

/**
 * Make the frame deterministic: build-time stamps ("Oct 22, 7:20 PM") and ages ("9d ago") depend on when the
 * fixture database was built, so they are rewritten to a fixed value of the same shape (the layout stays real), and
 * the sync pill is masked as well.
 */
async function steady(page: Page) {
  await page.evaluate(() => {
    const stamp = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{1,2}:\d{2}\s?(AM|PM)/g;
    const age = /\b\d+\s?(s|m|h|d|w|mo|y|min|mins|hours?|days?|weeks?) ago\b/g;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const parent = n.parentElement;
      if (!parent || parent.closest("script, style")) continue;
      const t = n.textContent ?? "";
      const next = t.replace(stamp, "Jan 1, 12:00 PM").replace(age, "1d ago");
      if (next !== t) n.textContent = next;
    }
    window.scrollTo(0, 0);
  });
}

const masks = (page: Page) => [page.locator(".sync-pill"), page.locator(".mobile-nav")];

// One test per theme, so each page carries a single theme init script: init scripts survive navigations and their
// order is undefined, so switching the theme on one page could draw a "-light" baseline in dark.
for (const route of ROUTES) {
  for (const theme of ["dark", "light"] as const) {
    test(`${route.name}: first screenful (${theme})`, async ({ page }, info) => {
      test.skip(!themesFor(info.project.name).includes(theme), `${theme} is not compared on ${info.project.name}`);
      await useTheme(page, theme);
      await open(page, route.path);
      if (route.name === "trade-result") {
        await expect(page.locator('main [class*="verdict"]').first()).toBeVisible({ timeout: 30_000 });
      }
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await steady(page);
      await expect(page).toHaveScreenshot(`${route.name}-${theme}.png`, { mask: masks(page) });
    });
  }
}

for (const theme of ["dark", "light"] as const) {
  test(`player chart (${theme})`, async ({ page }, info) => {
    test.skip(!info.project.name.includes("desktop"), "the chart is compared at desktop width only");
    test.skip(!themesFor(info.project.name).includes(theme), `${theme} is not compared on ${info.project.name}`);
    await useTheme(page, theme);
    await open(page, "/players?id=201");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await steady(page);
    const chart = page.locator("main figure").first();
    await chart.scrollIntoViewIfNeeded();
    await expect(chart).toHaveScreenshot(`player-chart-${theme}.png`);
  });
}
