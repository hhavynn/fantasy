import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page, type Request } from "@playwright/test";

// The Trade analyzer's scenario watchlist (issue #183) on the multi-league site (playwright.multileague.config.ts): the
// saved trades are device-local and league- and team-scoped, so they exist only under /l/<lg>/. Real scoped page and
// API, real browser storage. In the ESPN fixture league (scripts/build_e2e_db.py) team 2 rosters Kilo RB (207), Romeo
// WR (306) and Quebec WR (305); team 1 Foxtrot RB (202) and Oscar WR (303); team 3 Lima RB (208).
type Manifest = { leagues: Record<"espn_public" | "sleeper", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const listKey = (lg: string, team: number) => `fi:trade-scenarios:v1:${lg}:${team}`;

function watch(page: Page) {
  const errors: string[] = [];
  const writes: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r: Request) => {
    const url = new URL(r.url());
    // Nothing leaves for a provider, and nothing but GET reaches an API route.
    if (/espn\.com|sleeper\.(app|com)/.test(url.hostname)) writes.push(`provider: ${r.method()} ${r.url()}`);
    if (/\/api\//.test(url.pathname) && r.method() !== "GET") writes.push(`${r.method()} ${url.pathname}`);
  });
  return { errors, writes };
}

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function evaluateAndSave(page: Page, lg: string, query: string, n: number) {
  await page.goto(`/l/${lg}/trades?team=2&${query}`);
  const save = page.getByRole("button", { name: "Save to watchlist" });
  await expect(save).toBeVisible({ timeout: 30_000 });
  await save.click();
  await expect(page.getByText(`Saved as scenario ${n} on this device`)).toBeVisible();
}

for (const width of [1440, 375]) {
  test(`save, revisit and compare trade scenarios without crossing leagues (${width}px)`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width, height: 900 });
    const { espn_public: a, sleeper: b } = manifest().leagues;
    const seen = watch(page);

    // Three trades, each evaluated and saved once.
    await evaluateAndSave(page, a, "ta=2&tb=1&a=207&b=202", 1);
    await evaluateAndSave(page, a, "ta=2&tb=1&a=207&b=303", 2);
    await evaluateAndSave(page, a, "ta=2&tb=3&a=306&a=305&b=208", 3);

    // Revisit with no selection: all three are listed without reselecting a player.
    await page.goto(`/l/${a}/trades?team=2`);
    const list = page.getByRole("region", { name: "Trade scenario watchlist" });
    const items = list.getByRole("list", { name: "Saved trades" }).getByRole("listitem");
    await expect(items).toHaveCount(3);
    await expect(items.nth(0)).toContainText("Team 2 gives Kilo RB");
    await expect(items.nth(0)).toContainText("Team 1 gives Foxtrot RB");
    await expect(items.nth(2)).toContainText("Team 2 gives Romeo WR, Quebec WR");
    const stored = await page.evaluate((k) => JSON.parse(localStorage.getItem(k)!), listKey(a, 2));
    expect(stored.league).toBe(a);
    expect(stored.team).toBe(2);
    expect(Object.keys(stored.scenarios[0]).sort()).toEqual(["a", "b", "dataVersion", "id", "savedAt", "sw", "ta", "tb"]);

    // Reopen scenario 2 in the analyzer: the same selection, recognised as saved.
    await items.nth(1).getByRole("link", { name: "Open scenario 2 in the analyzer" }).click();
    await expect(page).toHaveURL(new RegExp(`/l/${a}/trades\\?.*ta=2&tb=1&a=207&b=303`));
    await expect(page.getByText("Saved as scenario 2 on this device")).toBeVisible({ timeout: 30_000 });

    // A stale scenario saved on older data: a player who has since moved (Lima RB is on team 3, not team 1).
    await page.evaluate(({ k }) => {
      const v = JSON.parse(localStorage.getItem(k)!);
      v.scenarios.push({ id: "stale1", ta: 2, tb: 1, a: [204], b: [208], sw: null, savedAt: "2026-09-01T12:00:00Z", dataVersion: "old-version" });
      localStorage.setItem(k, JSON.stringify(v));
    }, { k: listKey(a, 2) });
    await page.goto(`/l/${a}/trades?team=2`);
    await expect(items).toHaveCount(4);
    await expect(items.nth(3)).toContainText("Lima RB is no longer on Team 1 (now on Team 3)");
    await expect(items.nth(3)).toContainText("Saved on older league data");

    // Compare all four over one horizon on one snapshot.
    const answered = page.waitForResponse((r) => r.url().includes(`/l/${a}/api/trade-scenarios`));
    await list.getByRole("button", { name: "Compare 4 scenarios" }).click();
    const res = await answered;
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.contract).toBe("trade-scenario-compare/v1");
    expect(body.scope).toBe(a);
    expect(body.scenarios.map((r: { status: string }) => r.status)).toEqual(["evaluated", "evaluated", "evaluated", "stale"]);
    const table = page.getByRole("region", { name: "Scenario comparison" });
    await expect(table).toContainText(/weeks \d+–\d+ for every scenario/);
    await expect(table).toContainText("Scenarios 1 and 2 both include Kilo RB: at most one of them can happen as saved.");
    await expect(table).toContainText("Out of date, not re-valued: Lima RB is no longer on Team 1 (now on Team 3)");
    await expect(table).toContainText("the rows are alternatives and never add up");
    await expect(table.getByRole("rowgroup").filter({ has: page.getByText("Scenario 1", { exact: true }) }).getByRole("row")).toHaveCount(3);
    // Every link out of the comparison stays in this league.
    for (const href of await table.getByRole("link").evaluateAll((ls) => ls.map((l) => l.getAttribute("href") ?? ""))) {
      if (href.startsWith("/")) expect(href.startsWith(`/l/${a}/`)).toBe(true);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
    if (width === 375) await list.screenshot({ path: test.info().outputPath("trade-scenarios-mobile.png") });

    // Duplicate fills the list to five; saving is then refused until one is removed.
    await items.nth(0).getByRole("button", { name: "Duplicate scenario 1" }).click();
    await expect(items).toHaveCount(5);
    await expect(items.nth(0).getByRole("button", { name: "Duplicate scenario 1" })).toBeDisabled();
    await items.nth(4).getByRole("button", { name: "Remove scenario 5" }).click();
    await expect(items).toHaveCount(4);

    // Another league never sees league A's scenarios.
    await page.goto(`/l/${b}/trades`);
    const other = page.getByRole("region", { name: "Trade scenario watchlist" });
    await expect(other).toBeVisible({ timeout: 30_000 });
    await expect(other.getByRole("listitem")).toHaveCount(0);
    await expect(other).not.toContainText(/Kilo RB|Foxtrot RB|Lima RB/);

    expect(seen.writes, "no provider calls and no non-GET API requests").toEqual([]);
    expect(seen.errors, "console errors").toEqual([]);
  });
}

test("the comparison refuses an unscoped request and a forged league header", async ({ request }) => {
  const res = await request.get("/api/trade-scenarios?scenarios=2:1:207:202", { headers: { "x-fi-league": manifest().leagues.espn_public } });
  expect(res.status()).toBe(404);
});
