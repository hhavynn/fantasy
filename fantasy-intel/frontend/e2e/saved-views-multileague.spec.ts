import fs from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page, type Request } from "@playwright/test";

// Saved views and league-scoped search (issue #200) on the multi-league site (playwright.multileague.config.ts). Views
// are device-local and keyed by league and tool, so they exist only under /l/<lg>/. Real scoped pages and API, real
// browser storage. In the ESPN fixture league (backend/tests/espn_fixture.py) Waiver WR (309) is on waivers, Waiver RB
// (209) is a free agent and Kilo RB (207) is on team 2; the Sleeper league says only rostered or not.
type Manifest = { leagues: Record<"espn_public" | "sleeper", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const viewsKey = (lg: string, tool: string) => `fi:saved-views:v1:${lg}:${tool}`;

function watch(page: Page) {
  const errors: string[] = [];
  const writes: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r: Request) => {
    const url = new URL(r.url());
    if (/espn\.com|sleeper\.(app|com)/.test(url.hostname)) writes.push(`provider: ${r.method()} ${r.url()}`);
    if (/\/api\//.test(url.pathname) && r.method() !== "GET") writes.push(`${r.method()} ${url.pathname}`);
  });
  return { errors, writes };
}

/** The page's views panel, opened. It renders only with the real page (never in the loading skeleton). */
async function openViews(page: Page, label: string): Promise<Locator> {
  const box = page.getByRole("group", { name: `${label} views` });
  await expect(box).toBeVisible({ timeout: 30_000 });
  if ((await box.getAttribute("open")) === null) await box.locator("summary").click();
  await expect(box.getByRole("heading", { name: /Saved on this device/ })).toBeVisible();
  return box;
}

async function noPageOverflow(page: Page) {
  const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(scroll).toBeLessThanOrEqual(client);
}

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

for (const width of [1440, 375]) {
  test(`save and reopen views without leaving the league, never across leagues (${width}px)`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width, height: 900 });
    const { espn_public: a, sleeper: b } = manifest().leagues;
    const seen = watch(page);

    // Rankings in league A: save the current filters (the page offset is not a filter and is not saved).
    await page.goto(`/l/${a}/rankings?pos=RB&horizon=ros&offset=0`);
    let box = await openViews(page, "Rankings");
    await expect(box).toContainText("RB · Rest of season");
    await noPageOverflow(page);
    await box.getByLabel("Save current filters as").fill("RB rest of season");
    await box.getByRole("button", { name: "Save view" }).click();
    await expect(box.getByRole("status")).toHaveText("Saved “RB rest of season” on this device for this league.");
    const stored = await page.evaluate((k) => JSON.parse(localStorage.getItem(k)!), viewsKey(a, "rankings"));
    expect(stored).toEqual({ v: 1, league: a, tool: "rankings", views: [{ name: "RB rest of season", params: [["pos", "RB"], ["horizon", "ros"]] }] });

    // From the default view, the saved view reopens in the same league.
    await page.goto(`/l/${a}/rankings`);
    box = await openViews(page, "Rankings");
    await box.getByRole("link", { name: "RB rest of season", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/l/${a}/rankings\\?pos=RB&horizon=ros$`));
    // The panel's summary names the open view; inside, it is marked as the current one.
    await expect(page.getByRole("group", { name: "Rankings views" }).locator("summary")).toContainText("RB rest of season");
    box = await openViews(page, "Rankings");
    await expect(box.getByRole("link", { name: "RB rest of season", exact: true })).toHaveAttribute("aria-current", "page");

    // League B never sees it, even with league A's value copied under its own key.
    await page.goto(`/l/${b}/rankings`);
    box = await openViews(page, "Rankings");
    await expect(box).toContainText("None yet.");
    await expect(box.getByRole("link", { name: "RB rest of season", exact: true })).toHaveCount(0);
    expect(await page.evaluate((k) => localStorage.getItem(k), viewsKey(b, "rankings"))).toBeNull();
    await page.evaluate(({ from, to }) => localStorage.setItem(to, localStorage.getItem(from)!), { from: viewsKey(a, "rankings"), to: viewsKey(b, "rankings") });
    await page.reload();
    box = await openViews(page, "Rankings");
    await expect(box).toContainText("None yet.");
    await expect(box.getByRole("link", { name: "RB rest of season", exact: true })).toHaveCount(0);
    await noPageOverflow(page);

    // Waivers, a team-scoped page: the view holds filters only and reopens for the team being analyzed.
    await page.goto(`/l/${a}/waivers?team=2&view=ALL&rec=CLAIM&offset=30`);
    box = await openViews(page, "Waivers");
    await noPageOverflow(page);
    await box.getByLabel("Save current filters as").fill("Claims");
    await box.getByRole("button", { name: "Save view" }).click();
    await expect(box.getByRole("status")).toHaveText("Saved “Claims” on this device for this league.");
    expect(await page.evaluate((k) => JSON.parse(localStorage.getItem(k)!).views, viewsKey(a, "waivers")))
      .toEqual([{ name: "Claims", params: [["view", "ALL"], ["rec", "CLAIM"]] }]);
    await page.goto(`/l/${a}/waivers?team=2`);
    box = await openViews(page, "Waivers");
    await box.getByRole("link", { name: "Claims", exact: true }).click();
    await expect(page).toHaveURL((u) => u.pathname === `/l/${a}/waivers` && u.searchParams.get("team") === "2"
      && u.searchParams.get("view") === "ALL" && u.searchParams.get("rec") === "CLAIM" && !u.searchParams.has("offset"));
    await page.goto(`/l/${a}/waivers?team=2`);
    box = await openViews(page, "Waivers");
    await box.getByRole("button", { name: "Delete view Claims" }).click();
    await expect(box).toContainText("None yet.");
    expect(await page.evaluate((k) => localStorage.getItem(k), viewsKey(a, "waivers"))).toBeNull();

    // Trade discovery: the suggested view is recognised as the open one, and a saved view keeps the repeated shape.
    await page.goto(`/l/${a}/trades/discover?team=2&mode=fair&shape=1-for-1`);
    box = await openViews(page, "Trade discovery");
    await expect(box.getByRole("link", { name: "Fairer 1-for-1 swaps", exact: true })).toHaveAttribute("aria-current", "page");
    await box.getByLabel("Save current filters as").fill("Fair swaps");
    await box.getByRole("button", { name: "Save view" }).click();
    await expect(box.getByRole("status")).toHaveText("Saved “Fair swaps” on this device for this league.");
    expect(await page.evaluate((k) => JSON.parse(localStorage.getItem(k)!).views, viewsKey(a, "discover")))
      .toEqual([{ name: "Fair swaps", params: [["mode", "fair"], ["shape", "1-for-1"]] }]);
    await noPageOverflow(page);

    expect(seen.writes).toEqual([]);
    expect(seen.errors).toEqual([]);
  });

  test(`search says who holds a player in each league's own terms (${width}px)`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width, height: 900 });
    const { espn_public: a, sleeper: b } = manifest().leagues;
    const seen = watch(page);

    // Players search in the ESPN league: free agent, on waivers and rostered are told apart, each with its next step.
    await page.goto(`/l/${a}/players?q=Waiver`);
    const results = page.getByRole("table");
    const rowOf = (name: string) => results.getByRole("row").filter({ hasText: name });
    await expect(rowOf("Waiver WR")).toContainText("On waivers", { timeout: 30_000 });
    await expect(rowOf("Waiver RB")).toContainText("Free agent");
    await expect(rowOf("Waiver RB").getByRole("link", { name: "Simulate adding Waiver RB on Waivers" }))
      .toHaveAttribute("href", new RegExp(`^/l/${a}/waivers\\?(.*&)?add=209`));
    await expect(rowOf("Waiver RB").getByRole("link", { name: /Find trades/ })).toHaveCount(0);
    await noPageOverflow(page);
    await page.goto(`/l/${a}/players?q=Kilo`);
    await expect(rowOf("Kilo RB").getByRole("link", { name: "Find trades for Kilo RB" }))
      .toHaveAttribute("href", new RegExp(`^/l/${a}/trades/discover\\?mode=targeted&target=207`), { timeout: 30_000 });
    await expect(rowOf("Kilo RB").getByRole("link", { name: /Simulate adding/ })).toHaveCount(0);

    // The command palette answers from this league's rosters, with the same words.
    await page.keyboard.press("Control+k");
    const dialog = page.getByRole("dialog", { name: "Search" });
    await dialog.getByRole("combobox", { name: "Search players and pages" }).fill("Waiver");
    await expect(dialog.getByRole("option", { name: /^Waiver WR/ })).toContainText("On waivers");
    await expect(dialog.getByRole("option", { name: /^Waiver RB/ })).toContainText("Free agent");
    await page.keyboard.press("Escape");

    // The Sleeper league says only rostered or not: never "Free agent", never another league's holders.
    await page.goto(`/l/${b}/players?q=Waiver`);
    await expect(rowOf("Waiver WR")).toContainText("Not rostered", { timeout: 30_000 });
    await expect(results).not.toContainText("Free agent");
    await expect(results).not.toContainText("On waivers");
    await page.keyboard.press("Control+k");
    await dialog.getByRole("combobox", { name: "Search players and pages" }).fill("Waiver");
    await expect(dialog.getByRole("option", { name: /^Waiver WR/ })).toContainText("Not rostered");
    await expect(dialog.getByRole("listbox")).not.toContainText("Free agent");
    await page.keyboard.press("Escape");

    // Recent: a player picked in league A is remembered for league A only, by name and position, without A's holder,
    // roster status or injury, and none of it appears in the Sleeper league.
    await page.goto(`/l/${a}/players?q=Kilo`);
    await expect(rowOf("Kilo RB")).toBeVisible({ timeout: 30_000 });
    const input = dialog.getByRole("combobox", { name: "Search players and pages" });
    await page.keyboard.press("Control+k");
    await input.fill("Kilo");
    const kilo = dialog.getByRole("option", { name: /^Kilo RB/ });
    await expect(kilo).toBeVisible();
    const holder = (await kilo.textContent())!.split(" · ").pop()!.trim();
    expect(holder).not.toBe("");
    await kilo.click();
    await expect(page).toHaveURL(new RegExp(`/l/${a}/players\?id=207`));
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Kilo RB", { timeout: 30_000 });
    await page.keyboard.press("Control+k");
    await input.fill("Waiver WR");
    await dialog.getByRole("option", { name: /^Waiver WR/ }).click();
    await expect(page).toHaveURL(new RegExp(`/l/${a}/players\?id=309`));
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Waiver WR", { timeout: 30_000 });
    await page.keyboard.press("Control+k");
    const recentA = dialog.getByRole("group", { name: "Recent" });
    await expect(recentA.getByRole("option")).toHaveText([/^Waiver WR\s*WR$/, /^Kilo RB\s*RB$/]);
    await page.keyboard.press("Escape");
    const storedRecents = await page.evaluate((k) => localStorage.getItem(k), `fi:palette-recent:v1:${a}`);
    expect(storedRecents).not.toMatch(new RegExp(`waivers|Free agent|${holder}`, "i"));
    expect(await page.evaluate(() => localStorage.getItem("fantasy-intel:palette-recent"))).toBeNull();

    await page.goto(`/l/${b}/rankings`);
    await expect(page.getByRole("group", { name: "Rankings views" })).toBeVisible({ timeout: 30_000 });
    await page.keyboard.press("Control+k");
    await expect(input).toBeFocused();
    await expect(dialog.getByRole("group", { name: "Recent" })).toHaveCount(0);
    for (const text of ["Free agent", "On waivers", holder, "Kilo RB", "Waiver WR"]) await expect(dialog).not.toContainText(text);
    await page.keyboard.press("Escape");

    expect(seen.writes).toEqual([]);
    expect(seen.errors).toEqual([]);
  });
}
