import fs from "node:fs";
import path from "node:path";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import {
  applyTheme, collectErrors, expectAccessible, expectAppBarFits, expectNoBrokenValues, expectNoSidewaysScroll,
  expectStructure, open, stubPhotos, useTheme,
} from "./ux";

// #167 acceptance through the real Next server actions and FastAPI/control plane. The existing multi-league harness
// replaces only external provider reads and drain dispatch. Published dashboards use real synthetic DuckDB snapshots;
// no worker is run and no live provider is contacted. Users/league IDs are isolated by width, from other UX fixtures.
type Cookie = { name: string; value: string };
type User = { cookie: Cookie; states: Record<string, string>; privateId: string; deniedId: string };
type Manifest = { leagues: Record<string, string>; onboarding: { users: Record<string, User>; otherCookie: Cookie } };
const manifest = (): Manifest => JSON.parse(fs.readFileSync(
  path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const main = (page: Page) => page.getByRole("main");
const flow = (page: Page) => main(page).getByRole("region", { name: "Add a league", exact: true });
const dashboard = (page: Page) => main(page).getByRole("link", { name: "Open your dashboard", exact: true });
const progress = (id: string) => `/leagues?league=${id}`;
const S2 = "SYNTHETIC" + "not-a-real-cookie".repeat(3);
const SWID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}";

async function signIn(context: BrowserContext, cookie: Cookie) {
  await context.addCookies([{ ...cookie, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
}

async function quality(page: Page, scenario: string) {
  await expect(main(page).locator('[aria-busy="true"]')).toHaveCount(0);
  await expectStructure(page);
  await expectNoBrokenValues(page);
  for (const theme of ["light", "dark"] as const) {
    await applyTheme(page, theme);
    await expectNoSidewaysScroll(page);
    await expectAppBarFits(page);
    await expectAccessible(page, theme);
    const screenshot = test.info().outputPath(`${scenario}-${theme}.png`);
    await page.screenshot({ path: screenshot, mask: [page.locator('input[type="password"]')] });
    await test.info().attach(`${scenario}-${theme}`, { path: screenshot, contentType: "image/png" });
  }
}

async function espn(page: Page, id: string) {
  await open(page, "/leagues?host=espn");
  await main(page).getByLabel("ESPN league id or address").fill(id);
  await main(page).getByLabel("Season", { exact: true }).fill("2026");
  await main(page).getByRole("button", { name: "Add league", exact: true }).click();
}

test.use({ reducedMotion: "reduce", actionTimeout: 10_000 });
test.beforeEach(async ({ page }) => { await stubPhotos(page); await useTheme(page, "light"); });

for (const width of [320, 390, 768, 1440]) {
  test.describe(`onboarding ${width}px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("guest demo, validated public ESPN import, duplicate and Sleeper dashboard", async ({ page, context }) => {
      test.setTimeout(120_000);
      const errors = collectErrors(page);
      // No last-league cookie: the classic-retired landing redirects to connections and exposes the demo before auth.
      await open(page, "/");
      await expect(page).toHaveURL(/\/leagues$/);
      const demo = main(page).getByRole("link", { name: "Open the demo league" });
      await expect(demo).toBeVisible();
      await quality(page, "guest-demo-discovery");
      await demo.focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/leagues\/demo$/);
      await expect(main(page)).toContainText("Everything on this page is invented");
      await expect(main(page)).not.toContainText("Private Fixture League");
      await quality(page, "synthetic-demo");
      await main(page).getByRole("link", { name: "Connect your league" }).click();

      await signIn(context, manifest().onboarding.users[String(width)].cookie);
      await open(page, "/leagues");
      for (const host of ["ESPN", "Sleeper"]) {
        const card = main(page).getByRole("article", { name: host, exact: true });
        await card.locator("summary").click();
        await expect(card).toContainText(host === "ESPN" ? "given with consent" : "does not offer this");
      }
      await quality(page, "provider-limitations");
      await open(page, "/leagues?host=espn");
      const field = main(page).getByLabel("ESPN league id or address");
      await field.fill("invalid-id");
      await main(page).getByRole("button", { name: "Add league", exact: true }).click();
      await expect(field).toHaveValue("invalid-id");
      expect(await field.evaluate((el: HTMLInputElement) => el.validity.patternMismatch)).toBe(true);
      await expect(page).toHaveURL(/host=espn$/);
      await quality(page, "malformed-id-kept");
      // A valid but nonexistent ID reaches the real server action/API and remains correctable in place.
      await field.fill("999999998");
      await main(page).getByLabel("Season", { exact: true }).fill("2026");
      await field.press("Enter");
      await expect(main(page).getByRole("alert")).toContainText("ESPN has no league with that id");
      await expect(field).toHaveValue("999999998");
      await quality(page, "provider-not-found");

      await field.fill("https://fantasy.espn.com/football/league?leagueId=999&seasonId=2026");
      await field.press("Enter");
      const ready = manifest().leagues.espn_public;
      await expect(page).toHaveURL(new RegExp(`league=${ready}$`));
      await expect(dashboard(page)).toHaveAttribute("href", `/l/${ready}`);
      await quality(page, "public-ready");
      await dashboard(page).click();
      await expect(page).toHaveURL(new RegExp(`/l/${ready}$`));
      await expect(main(page)).toContainText("Pick a team");
      await main(page).getByRole("combobox", { name: "Analyzing", exact: true }).selectOption({ index: 1 });
      await expect(page).toHaveURL(new RegExp(`/l/${ready}\\?team=1$`));
      await expect(main(page).getByRole("heading", { name: "Before kickoff", exact: true })).toBeVisible();
      await expect(main(page).getByText(/^Next step ·/)).toBeVisible();
      await quality(page, "first-authorized-decision");
      await espn(page, "999");
      await expect(page).toHaveURL(new RegExp(`league=${ready}&already=1$`));
      await expect(main(page)).toContainText("Nothing was added twice");
      await quality(page, "duplicate-ready");

      await open(page, "/leagues?host=sleeper");
      await expect(main(page)).toContainText("Sleeper leagues are public, so no cookies are needed");
      await expect(main(page).locator('input[type="password"]')).toHaveCount(0);
      await main(page).getByLabel("Sleeper league id or address").fill("https://sleeper.com/leagues/1190000000000000001");
      await main(page).getByRole("button", { name: "Add league", exact: true }).click();
      const sleeper = manifest().leagues.sleeper;
      await expect(page).toHaveURL(new RegExp(`league=${sleeper}$`));
      await expect(dashboard(page)).toHaveAttribute("href", `/l/${sleeper}`);
      await quality(page, "sleeper-ready");
      expect(errors, "console errors").toEqual([]);
    });

    test("private ESPN requires consent, denies non-owners, resumes and allows leaving a first sync", async ({ page, context }) => {
      test.setTimeout(120_000);
      const user = manifest().onboarding.users[String(width)];
      await signIn(context, user.cookie);
      const errors = collectErrors(page);
      await espn(page, user.deniedId);
      await expect(page).toHaveURL(new RegExp(`host=espn&private=${user.deniedId}&season=2026$`));
      await expect(main(page)).toContainText("your own, never someone else's");
      await expect(main(page)).toContainText("stored encrypted");
      await main(page).getByLabel("espn_s2", { exact: true }).fill(S2);
      await main(page).getByLabel("SWID", { exact: true }).fill(SWID);
      const consent = main(page).getByRole("checkbox");
      await main(page).getByRole("button", { name: "Connect privately", exact: true }).click();
      expect(await consent.evaluate((el: HTMLInputElement) => el.validity.valueMissing)).toBe(true);
      await expect(page).toHaveURL(new RegExp(`private=${user.deniedId}`));
      await quality(page, "private-consent-required");
      await consent.check();
      await main(page).getByRole("button", { name: "Connect privately", exact: true }).click();
      await expect(main(page).getByRole("alert")).toContainText("does not own a team");
      await expect(main(page)).toContainText("invite link");
      await expect(main(page)).not.toContainText(S2);
      await expect(main(page)).not.toContainText(SWID);
      await expect(dashboard(page)).toHaveCount(0);
      await quality(page, "private-access-denied");

      await espn(page, user.privateId);
      await expect(page).toHaveURL(new RegExp(`private=${user.privateId}`));
      await main(page).getByLabel("espn_s2", { exact: true }).fill(S2);
      await main(page).getByLabel("SWID", { exact: true }).fill(SWID);
      await main(page).getByRole("checkbox").check();
      await main(page).getByRole("button", { name: "Connect privately", exact: true }).click();
      await expect(page).toHaveURL(/\/leagues\?league=lg_[A-Za-z0-9_-]+$/);
      const resume = page.url();
      await expect(main(page).getByRole("status").filter({ hasText: "Your league is in the queue" })).toBeVisible();
      await expect(dashboard(page)).toHaveCount(0);
      await page.reload();
      await expect(page).toHaveURL(resume);
      await expect(main(page)).toContainText("Your league is in the queue");
      await expect(main(page)).not.toContainText(S2);
      await quality(page, "private-queued-reload");
      // Private membership itself is listed without a pin. Pin explicitly before testing the separate unpin control.
      const card = page.locator("#your-leagues article").filter({ hasText: "Synthetic Private Import" });
      await card.getByRole("button", { name: "Pin", exact: true }).click();
      await expect(card).toContainText("Pinned.");
      await page.reload();
      await main(page).getByRole("button", { name: "Unpin this league", exact: true }).click();
      await expect(main(page)).toContainText("Unpinned (you are still a member)");
      await expect(dashboard(page)).toHaveCount(0);
      await quality(page, "leave-sync-with-membership-retained");
      expect(errors, "console errors").toEqual([]);
    });

    test("sync status matrix, recovery, reload and cross-user isolation", async ({ page, context }) => {
      test.setTimeout(180_000);
      const { onboarding } = manifest();
      const user = onboarding.users[String(width)];
      await signIn(context, user.cookie);
      const errors = collectErrors(page);
      for (const [state, headline, detail] of [
        ["no_data", "No sync has started yet", "Start one with Try again"],
        ["queued", "Your league is in the queue", "Waiting for a sync worker"],
        ["running", "Reading your league from ESPN", "Teams, rosters, scoring and matchups"],
        ["queued_stalled", "The first sync is taking much longer than usual", "No sync worker has picked it up for over an hour"],
        ["running_stalled", "The first sync is taking much longer than usual", "It started over an hour ago"],
        ["failed", "The first sync stopped", "ESPN did not answer"],
        ["empty", "The first sync stopped", "ESPN returned this league with no teams yet"],
        ["access", "The first sync stopped", "ESPN refused access"],
      ]) {
        await open(page, progress(user.states[state]));
        await expect(flow(page).getByRole("status").filter({ hasText: headline })).toBeVisible();
        await expect(flow(page)).toContainText(detail);
        await expect(dashboard(page)).toHaveCount(0);
        await expect(main(page).getByRole("button", { name: "Check now", exact: true })).toBeVisible();
        if (["no_data", "queued_stalled", "running_stalled", "failed", "empty", "access"].includes(state)) {
          await expect(flow(page).getByRole("button", { name: "Try again", exact: true })).toBeVisible();
        }
        if (state === "access") {
          await expect(flow(page).getByRole("link", { name: "Store fresh ESPN cookies" })).toHaveAttribute("href", `/leagues/${user.states.access}`);
        }
        await quality(page, state);
      }
      // Retry the no-job league through the real sync endpoint, then reload: no optimistic/premature ready state.
      await open(page, progress(user.states.no_data));
      await main(page).getByRole("button", { name: "Try again", exact: true }).click();
      await expect(main(page)).toContainText("Your league is in the queue");
      await page.reload();
      await expect(main(page)).toContainText("Your league is in the queue");
      await expect(dashboard(page)).toHaveCount(0);
      await quality(page, "refresh-started-real-job");

      await open(page, progress(user.states.failed));
      await flow(page).getByRole("button", { name: "Try again", exact: true }).click();
      await expect(flow(page)).toContainText("Your league is in the queue");
      await expect(dashboard(page)).toHaveCount(0);
      await quality(page, "failed-sync-retried");

      const stale = manifest().leagues.espn_stale;
      await open(page, progress(stale));
      await expect(flow(page)).toContainText("Your dashboard is open, with the last data synced");
      await expect(dashboard(page)).toHaveAttribute("href", `/l/${stale}`);
      await quality(page, "ready-with-stale-data");

      await context.clearCookies();
      await signIn(context, onboarding.otherCookie);
      await open(page, progress(user.states.running));
      await expect(main(page)).toContainText("This league is not available to you");
      await expect(main(page)).not.toContainText(`Onboarding ${width}`);
      await expect(dashboard(page)).toHaveCount(0);
      const refused = await main(page).innerText();
      await open(page, progress("lg_NeverCreatedLeague0000"));
      expect(await main(page).innerText(), "unknown and another user's private league are indistinguishable").toBe(refused);
      await quality(page, "other-user-refused");
      expect(errors, "console errors").toEqual([]);
    });
  });
}
