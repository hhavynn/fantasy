import fs from "node:fs";
import path from "node:path";
import { expect, test, type BrowserContext } from "@playwright/test";
import { collectErrors, expectAccessible, expectAppBarFits, expectNoBrokenValues, expectNoSidewaysScroll, expectStructure, open } from "./ux";

// Cross-league player exposure (issue #209) on /leagues/portfolio, run by playwright.multileague.config.ts. The viewer
// (scripts/e2e_multileague.py) follows the private ESPN fixture league with a saved team, a stale league without one and
// a never-synced league: the page counts the one league it can read, lists every player of the saved team with an exact
// "1 of 1", and says in words why the other two are not counted. Each check waits for real rows, not the skeleton.
type Manifest = { leagues: Record<string, string>; cookie: { name: string; value: string } };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));

async function signIn(context: BrowserContext) {
  const { cookie } = manifest();
  await context.addCookies([{ name: cookie.name, value: cookie.value, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
}

for (const width of [1440, 375, 320]) {
  test.describe(`player exposure at ${width}px`, () => {
    test.use({ viewport: { width, height: width < 720 ? 800 : 900 } });

    test(`exact counts, every league's state in words and league-scoped drilldowns (${width}px)`, async ({ page, context }) => {
      await signIn(context);
      const errors = collectErrors(page);
      await open(page, "/leagues/portfolio");
      await expect(page.locator("main h1")).toHaveText("Player exposure");
      const players = page.locator("#players table tbody tr");
      await expect(players.first()).toBeVisible({ timeout: 20_000 });          // real rows, not the loading skeleton
      expect(await players.count()).toBeGreaterThan(5);
      await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
      await expect(page.getByText("Leagues counted")).toBeVisible();
      await expect(page.locator("main")).toContainText("1 of 3");
      await expect(page.locator("main")).toContainText("2 leagues not counted");

      const leagues = page.locator("#leagues");
      await expect(leagues).toContainText("Private Fixture League");
      await expect(leagues.locator("li").filter({ hasText: "Stale Sync League" })).toContainText("No team saved");
      await expect(leagues.locator("li").filter({ hasText: "Blank Sync League" })).toContainText(/Not ready|no synced data|first sync/i);

      await expect(page.locator("#weeks table tbody tr").first()).toBeVisible();
      await expectStructure(page);
      await expectNoBrokenValues(page);
      await expectNoSidewaysScroll(page);
      await expectAppBarFits(page);
      await expectAccessible(page);

      // Drill into the first player's leagues: the link stays in that league.
      const lg = manifest().leagues.espn_private;
      const first = players.first();
      await expect(first.locator("td").nth(2)).toContainText("1 of 1");
      await first.locator("summary").click();
      const link = first.getByRole("link", { name: "Private Fixture League" });
      await expect(link).toHaveAttribute("href", new RegExp(`^/l/${lg}/players\\?id=-?\\d+$`));
      expect(errors, "console errors").toEqual([]);
    });
  });
}

test("search and sort are URL-backed and keep the page's own state", async ({ page, context }) => {
  await signIn(context);
  await open(page, "/leagues/portfolio?sort=name&dir=asc");
  const names = page.locator("#players tbody tr td:first-child span:first-child");
  await expect(names.first()).toBeVisible();
  const all = await names.allTextContents();
  expect(all, "sorted by name").toEqual([...all].sort((a, b) => a.localeCompare(b)));
  const query = all[0].split(" ")[0];
  await page.getByLabel("Search players, teams or leagues").fill(query);
  await page.locator("#players form[role=search] button[type=submit]").click();
  await expect(page).toHaveURL(/q=/);
  await expect(page).toHaveURL(/sort=name/);
  await expect(names.first()).toContainText(query);
});

test("signed out, the page asks to sign in and shows no player", async ({ page }) => {
  await open(page, "/leagues/portfolio");
  await expect(page.getByText("Sign in to see your players across leagues")).toBeVisible();
  await expect(page.locator("#players")).toHaveCount(0);
  await expectNoSidewaysScroll(page);
});
