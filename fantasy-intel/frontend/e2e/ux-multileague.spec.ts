import fs from "node:fs";
import path from "node:path";
import { expect, test, type BrowserContext } from "@playwright/test";
import {
  collectErrors, expectAccessible, expectAppBarFits, expectDecisionFirst, expectNoBrokenValues, expectNoSidewaysScroll,
  expectStructure, open, stubPhotos,
} from "./ux";

// UX acceptance gates on the multi-league site (issue #202), run by playwright.multileague.config.ts beside the
// parity matrix. Provider fixtures (scripts/e2e_multileague.py): a private ESPN league the viewer is signed in to, a
// public Sleeper league (superflex, FLEX and TE premium: the complex roster slots), a league whose sync paused after a
// publish a week earlier (stale) and one connected but never synced (blank). Each core destination at 320, 390 and
// 1440 for ESPN private (768 runs on the demo league in ux-gates.spec.ts), 320 and 1440 for Sleeper: one h1 and the
// landmarks, decision first, no sideways scroll, an app bar that fits (the league switcher included), no leaked
// undefined/NaN/null or engine identifier, axe-core clean or triaged, no console error.
type Kind = "espn_public" | "espn_private" | "sleeper" | "espn_stale" | "espn_blank";
type Manifest = { leagues: Record<Kind, string>; cookie: { name: string; value: string } };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));

async function signIn(context: BrowserContext) {
  const { cookie } = manifest();
  await context.addCookies([{ name: cookie.name, value: cookie.value, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
}

const CORE = [
  { name: "Overview", path: "/" },
  { name: "Start/Sit", path: "/start-sit", verdict: true },
  { name: "Matchup", path: "/matchup" },
  { name: "Waivers", path: "/waivers", verdict: true },
  { name: "Trades", path: "/trades" },
  { name: "Rankings", path: "/rankings" },
  { name: "Player profile", path: "/players?id=201" },
  { name: "Weekly recap", path: "/weekly-recap" },
];

const MATRIX = [
  { kind: "espn_private", signedIn: true, widths: [320, 390, 1440] },
  { kind: "sleeper", signedIn: false, widths: [320, 1440] },
] as const;

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
});

for (const { kind, signedIn, widths } of MATRIX) {
  for (const width of widths) {
    test.describe(`${kind} at ${width}px`, () => {
      test.use({ viewport: { width, height: width < 720 ? 800 : 900 } });
      for (const route of CORE) {
        test(`${kind} ${route.name} ${width}px`, async ({ page, context }) => {
          if (signedIn) await signIn(context);
          const errors = collectErrors(page);
          await open(page, `/l/${manifest().leagues[kind]}${route.path === "/" ? "" : route.path}`);
          await expectStructure(page);
          await expectNoBrokenValues(page);
          await expectDecisionFirst(page, { verdict: !!route.verdict });
          await expectNoSidewaysScroll(page);
          await expectAppBarFits(page);
          await expectAccessible(page);
          expect(errors, "console errors").toEqual([]);
        });
      }
    });
  }
}

for (const width of [320, 1440]) {
  test.describe(`league fixtures at ${width}px`, () => {
    test.use({ viewport: { width, height: 800 } });

    test(`connections page: each league's sync state in words, not colour alone (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      await open(page, "/leagues");
      const main = page.locator("main");
      for (const [name, state] of [
        ["Private Fixture League", "Up to date"],
        ["Stale Sync League", "Sync paused"],
        ["Blank Sync League", "No data yet"],
      ] as const) {
        const row = main.locator("li, tr, article").filter({ hasText: name }).first();
        await expect(row, `${name} is listed`).toBeVisible();
        await expect(row, `${name} says ${state}`).toContainText(state);
      }
      await expect(page.locator("h1")).toHaveCount(1);
      await expectNoSidewaysScroll(page);
      await expectAppBarFits(page);
      await expectAccessible(page);
      expect(errors, "console errors").toEqual([]);
    });

    test(`league settings for a never-synced league (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      await open(page, `/leagues/${manifest().leagues.espn_blank}`);
      await expect(page.locator("main h1")).toHaveText("Blank Sync League");
      await expect(page.locator("main")).toContainText("No data yet");
      await expectNoSidewaysScroll(page);
      await expectAccessible(page);
      expect(errors, "console errors").toEqual([]);
    });

    test(`a stale league still serves its last data (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      for (const p of ["", "/waivers"]) {
        await open(page, `/l/${manifest().leagues.espn_stale}${p}`);
        await expectStructure(page);
        await expectNoBrokenValues(page);
        await expectNoSidewaysScroll(page);
        await expectAppBarFits(page);
        await expectAccessible(page);
      }
      expect(errors, "console errors").toEqual([]);
    });

    test(`a never-synced league renders its pages without breaking the layout (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      for (const p of ["", "/start-sit", "/waivers"]) {
        await open(page, `/l/${manifest().leagues.espn_blank}${p}`);
        await expect(page.locator("h1"), "exactly one h1").toHaveCount(1);
        await expectNoSidewaysScroll(page);
        await expectAppBarFits(page);
        await expectAccessible(page);
      }
      expect(errors, "console errors").toEqual([]);
    });
  });
}

// The all-leagues action center (issue #166) on /leagues: the viewer follows a ready league with a saved team, a stale
// league without one and a never-synced league. Each gets a card that says its state in words (never a clean roster for
// the two without data to check), and the ready league's card deep-links into that league, team and week.
for (const width of [320, 1440]) {
  test.describe(`action center at ${width}px`, () => {
    test.use({ viewport: { width, height: 800 } });

    test(`action center: every league, its state in words and a working deep link (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      await open(page, "/leagues");
      const center = page.locator("#action-center");
      await expect(center.getByRole("heading", { name: "Across your leagues" })).toBeVisible();
      const card = (name: string) => center.locator("article").filter({ hasText: name });
      await expect(card("Private Fixture League")).toContainText("Lineup lock:");
      await expect(card("Stale Sync League")).toContainText("Team not chosen");
      await expect(card("Stale Sync League")).toContainText("Sync paused");
      await expect(card("Blank Sync League")).toContainText("No data yet");
      for (const name of ["Stale Sync League", "Blank Sync League"]) {
        await expect(card(name).locator("ul"), `${name} shows no decisions`).toHaveCount(0);
      }
      await expectNoSidewaysScroll(page);
      await expectAppBarFits(page);
      await expectAccessible(page);
      const lg = manifest().leagues.espn_private;
      const open_ = card("Private Fixture League").getByRole("link", { name: "Open league →" });
      await expect(open_).toHaveAttribute("href", new RegExp(`^/l/${lg}\\?team=1$`));
      await open_.click();
      await expect(page).toHaveURL(new RegExp(`/l/${lg}\\?team=1$`));
      await expect(page.locator("h1")).toHaveCount(1);
      expect(errors, "console errors").toEqual([]);
    });
  });
}

// Known defect, found by this gate: a connected league with no data yet shows the generic "The backend returned an
// error (409)" box and a "Backend offline" sync pill instead of first-sync guidance (PRODUCT.md: first run is never an
// error box). Un-fixme once the page and the pill handle the 409 "no data yet" answer.
test.fixme("a never-synced league explains its first sync instead of showing an error", async ({ page, context }) => {
  await signIn(context);
  await open(page, `/l/${manifest().leagues.espn_blank}`);
  await expect(page.locator("main")).not.toContainText(/The backend returned an error/);
  await expect(page.getByText("Backend offline")).toHaveCount(0);
});

test("league switching works from the keyboard and re-renders the header for the new league", async ({ page, context }) => {
  await signIn(context);
  const { espn_private: a, sleeper: b } = manifest().leagues;
  await open(page, `/l/${b}`);                  // remembered as a recent league
  await open(page, `/l/${a}/waivers`);
  const switcher = page.locator(".league-switcher > summary");
  await expect(switcher).toContainText("Private Fixture League");
  await switcher.focus();
  await expect(switcher).toBeFocused();
  await page.keyboard.press("Enter");
  const item = page.getByRole("menuitem", { name: "Sleeper Fixture League" });
  await expect(item).toBeVisible();
  await item.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/l/${b}$`));
  await expect(page.locator("main h1")).toHaveText(/\S/);
  await expect(page.locator(".league-switcher > summary")).toContainText("Sleeper Fixture League");
});
