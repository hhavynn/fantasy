import fs from "node:fs";
import path from "node:path";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { ALL_NAV_PAGES } from "../lib/navigation";

// The parity matrix (multi-league Phase 5): every page of the site under /l/<lg>/ for an ESPN public league
// (anonymous), an ESPN private league (signed in as its member) and a Sleeper league (anonymous). Each renders with
// HTTP 200, one h1, no backend or render error and no console error; the Sleeper league names no ESPN; a signed-out
// visitor never sees the private league. Data: scripts/e2e_multileague.py (manifest written before the servers start).
type Manifest = { leagues: Record<"espn_public" | "espn_private" | "sleeper", string>; cookie: { name: string; value: string } };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));

// Owner-only research pages need a platform admin: for these visitors they answer "not found", checked separately. The set
// comes from the nav's ownerOnly flags, so a new owner page is never added to the member parity matrix.
const OWNER_ONLY = new Set(ALL_NAV_PAGES.filter((p) => p.ownerOnly).map((p) => p.href));
const PAGES = [...ALL_NAV_PAGES.map((p) => p.href).filter((h) => !OWNER_ONLY.has(h)), "/managers/2", "/players?id=201"];
const FAILURE_TEXT = /Can't reach the backend|The backend returned an error|database schema is out of date|Something went wrong|This page couldn't be rendered|League not found/;
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}

async function signIn(context: BrowserContext) {
  const { cookie } = manifest();
  await context.addCookies([{ name: cookie.name, value: cookie.value, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
}

const KINDS = [
  { kind: "espn_public", signedIn: false, espnAllowed: true },
  { kind: "espn_private", signedIn: true, espnAllowed: true },
  { kind: "sleeper", signedIn: false, espnAllowed: false },
] as const;

for (const { kind, signedIn, espnAllowed } of KINDS) {
  for (const page_ of PAGES) {
    test(`${kind} ${page_}`, async ({ page, context }) => {
      if (signedIn) await signIn(context);
      await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
      const errors = collectErrors(page);
      const lg = manifest().leagues[kind];
      const res = await page.goto(`/l/${lg}${page_ === "/" ? "" : page_}`, { waitUntil: "load" });
      expect(res?.status(), "HTTP status").toBe(200);
      await page.waitForLoadState("networkidle");
      await expect(page.locator("h1"), "exactly one h1").toHaveCount(1);
      await expect(page.locator("main"), "no backend or render error").not.toContainText(FAILURE_TEXT);
      if (!espnAllowed) {
        const hits = (await page.locator("body").innerText()).split("\n").filter((l) => /\bESPN\b/.test(l));
        expect(hits, "no ESPN wording on a Sleeper league's page").toEqual([]);
      }
      expect(errors, "console errors").toEqual([]);
    });
  }
}

test("a signed-out visitor never sees the private league", async ({ page }) => {
  const lg = manifest().leagues.espn_private;
  await page.goto(`/l/${lg}/waivers`, { waitUntil: "load" });
  await expect(page.locator("main")).not.toContainText("Private Fixture League");
  await expect(page.locator("main")).not.toContainText("Echo RB");
});

test("owner-only research pages are not shown to a member who is not a platform admin", async ({ page, context }) => {
  await signIn(context);
  const lg = manifest().leagues.espn_private;
  for (const p of OWNER_ONLY) {
    await page.goto(`/l/${lg}${p}`, { waitUntil: "load" });
    await expect(page.locator("main")).not.toContainText(/MAE|promotion gate|Research Lab pool/);
  }
  await page.goto(`/l/${lg}/rankings`, { waitUntil: "load" });         // the Players group, where both live
  await expect(page.getByRole("link", { name: "Research lab" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Projection accuracy" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Game splits" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Pace lab" })).toHaveCount(0);
  // The 404 page lists the pages a visitor may open: the same filter as the nav.
  await page.goto(`/l/${lg}/no-such-page`, { waitUntil: "load" });
  const pages = page.locator("main").getByRole("navigation", { name: "All pages" });
  await expect(pages.getByRole("link", { name: "Rankings", exact: true })).toBeVisible();
  for (const p of ALL_NAV_PAGES.filter((q) => q.ownerOnly)) {
    await expect(pages.getByRole("link", { name: p.label, exact: true }), p.href).toHaveCount(0);
  }
});

test("the terms, the privacy policy and account settings render, unscoped, for visitors and members", async ({ page, context }) => {
  const errors = collectErrors(page);
  for (const [href, h1] of [["/terms", "Terms"], ["/privacy", "Privacy policy"]] as const) {
    const res = await page.goto(href, { waitUntil: "load" });
    expect(res?.status(), href).toBe(200);
    expect(new URL(page.url()).pathname).toBe(href);
    await expect(page.locator("h1")).toHaveText(h1);
    // No league here: the header shows no sync state (the retired classic status route is never asked).
    await expect(page.getByText("Backend offline")).toHaveCount(0);
  }
  await page.goto("/account", { waitUntil: "load" });
  await expect(page.locator("main")).toContainText("Not signed in");
  await signIn(context);
  await page.goto("/account", { waitUntil: "load" });
  await expect(page.locator("main")).toContainText("viewer@example.test");
  await expect(page.getByRole("button", { name: "Delete my account" })).toBeVisible();
  expect(errors).toEqual([]);
});

test("a link across the league boundary re-renders the header, both ways", async ({ page, context }) => {
  await signIn(context);
  const lg = manifest().leagues.espn_private;
  await page.goto(`/l/${lg}/waivers`, { waitUntil: "load" });
  await expect(page.locator("button.sync-pill")).toBeVisible();
  // League page -> /terms by a client-side link (the account menu): the league header goes.
  await page.locator(".account-menu summary").click();
  await page.getByRole("menuitem", { name: "Terms" }).click();
  await expect(page).toHaveURL(/\/terms$/);
  await expect(page.locator("button.sync-pill")).toHaveCount(0);
  // /leagues -> a league by a client-side link: the league header comes back.
  await page.goto("/leagues", { waitUntil: "load" });
  await expect(page.locator("button.sync-pill")).toHaveCount(0);
  // The league list's own link: the action center above it ("Across your leagues", streamed in) links the same
  // league with ?team=, so the region is matched exactly.
  await page.getByRole("region", { name: "Your leagues", exact: true }).getByRole("link", { name: "Private Fixture League", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/l/${lg}$`));
  await expect(page.locator("button.sync-pill")).toBeVisible();
});

// League-scoped links (#262 and its part 2): with JavaScript off, a league page's server-rendered links, form actions
// and form-submit buttons already carry /l/<lg>/. Only hrefs that belong to no league (account pages, the connections
// page, assets) and the switcher's ?league=clear stay bare. The last-viewed-league cookie names another league only to
// show that the rendered hrefs do not depend on it; nothing is clicked here. Waiver files edited by #264 keep next/link
// until it merges, but /waivers' own links are query-relative (?pos=...), so the page is listed.
const UNSCOPED_HREF = /^\/(leagues|login|signup|auth|invite|account|admin|terms|privacy|api|_next)(\/|\?|#|$)/;
test("with JavaScript off, a league page renders its links and forms in that league", async ({ browser }) => {
  const { espn_public: lg, sleeper: other } = manifest().leagues;
  const context = await browser.newContext({ javaScriptEnabled: false });
  await context.addCookies([{ name: "fi_league", value: other, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
  const page = await context.newPage();
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const bare = async () => (await page.locator("a[href^='/'], form[action^='/'], [formaction^='/']").evaluateAll((els) =>
    els.map((e) => e.getAttribute("href") ?? e.getAttribute("action") ?? e.getAttribute("formaction") ?? "")))
    .filter((u) => !u.startsWith("//") && !u.startsWith(`/l/${lg}`) && !UNSCOPED_HREF.test(u) && !/[?&]league=clear\b/.test(u));
  // The Overview twice: with no team (no hub, #165) and from a team's perspective, so the weekly hub's links are checked
  const pages = ["/rankings", "/managers", "/", "/?team=1", "/players?id=201", "/start-sit", "/transactions",
    "/trades/discover", "/luck-lab", "/movers", "/waivers"];
  for (const p of pages) {
    const res = await page.goto(`/l/${lg}${p === "/" ? "" : p.startsWith("/?") ? p.slice(1) : p}`, { waitUntil: "load" });
    expect(res?.status(), `${p} HTTP status`).toBe(200);
    expect(await bare(), `${p}: in-app links and form actions outside /l/<lg>/`).toEqual([]);
  }
  await page.goto(`/l/${lg}/rankings`, { waitUntil: "load" });
  expect(await page.locator(`a[href^='/l/${lg}/players?id=']`).count(), "player links scoped to the page's league").toBeGreaterThan(0);
  // The no-JS search form (#262) is scoped, not merely absent.
  expect(await page.locator(`form[action^='/l/${lg}/rankings']`).count(), "the search form posts to this league").toBeGreaterThan(0);
  // ?league=clear on a league URL redirects to the same page without it, so no link built from the page's params carries
  // it (the league switcher's own "Classic site" link, `/?league=clear`, is the deliberate way out and is not counted).
  await page.goto(`/l/${lg}/start-sit?league=clear`, { waitUntil: "load" });
  expect(new URL(page.url()).pathname).toBe(`/l/${lg}/start-sit`);
  expect(await page.locator("a[href*='league=clear']:not([href='/?league=clear']), option[value*='league=clear']").count(),
    "no link built from the page's params leaves the league").toBe(0);
  await context.close();
});

// With JavaScript on, /l/<lg>/<page>?league=clear lands on the same league page without the param (a redirect, so the
// browser URL that the Nav team picker and tab memory read through useSearchParams() no longer carries it), keeps the
// other params, and keeps the league cookie on this league.
test("?league=clear on a league URL stays in that league, browser URL included", async ({ page, context }) => {
  const { espn_public: lg, sleeper: other } = manifest().leagues;
  await context.addCookies([{ name: "fi_league", value: other, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const res = await page.goto(`/l/${lg}/start-sit?week=1&league=clear`, { waitUntil: "load" });
  expect(res?.status(), "HTTP status").toBe(200);
  const url = new URL(page.url());
  expect(url.pathname).toBe(`/l/${lg}/start-sit`);
  expect(url.searchParams.getAll("league"), "no league param left").toEqual([]);
  expect(url.searchParams.get("week")).toBe("1");
  const cookie = (await context.cookies()).find((c) => c.name === "fi_league");
  expect(cookie?.value, "the league cookie names this league").toBe(lg);
});

// Player profile tabs (#187): a deep-linked tab, a tab switch and a week window all stay under /l/<lg>/, and the
// profile's Save writes only that league's device-local shortlist.
test("a player profile tab keeps its league, and Save writes only that league's shortlist", async ({ page }) => {
  const { espn_public: a, sleeper: b } = manifest().leagues;
  // Fixture players have no real ESPN photo: stub the CDN, as the parity matrix above does.
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const errors = collectErrors(page);
  // Name any missing resource, so a console 404 says which request it was.
  page.on("response", (r) => { if (r.status() === 404) errors.push(`404: ${r.url()}`); });
  await page.goto(`/l/${a}/players?id=209&tab=usage`, { waitUntil: "load" });
  const tabs = page.getByRole("tablist", { name: /profile$/ });
  await expect(tabs.getByRole("tab", { name: "Usage", exact: true })).toHaveAttribute("aria-selected", "true");
  await tabs.getByRole("tab", { name: "Game log", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/l/${a}/players\\?id=209&tab=game-log$`));
  await page.getByRole("group", { name: "Weeks shown" }).getByRole("link", { name: "Last 3 weeks" }).click();
  await expect(page).toHaveURL(new RegExp(`/l/${a}/players\\?id=209&tab=game-log&weeks=3$`));
  const save = page.locator("main").getByRole("button", { name: /^Save .* to shortlist$/ });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator("main").getByRole("button", { name: /^Remove .* from shortlist$/ })).toHaveAttribute("aria-pressed", "true");
  const stored = await page.evaluate(([ka, kb]) => [localStorage.getItem(ka), localStorage.getItem(kb)],
    [`fi:waiver-shortlist:v1:${a}`, `fi:waiver-shortlist:v1:${b}`]);
  expect(stored).toEqual(["[209]", null]);
  // A rostered player has nothing to save for.
  await page.goto(`/l/${a}/players?id=201`, { waitUntil: "load" });
  await expect(page.getByRole("tablist", { name: /profile$/ }).getByRole("tab", { name: "Overview", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("main").getByRole("button", { name: /shortlist$/ })).toHaveCount(0);
  expect(errors, "console errors").toEqual([]);
});

// Shortlist uses the real scoped page/API and browser storage; no mocked waiver responses.
for (const width of [1440, 375]) {
  test(`shortlist save reload remove and league isolation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const { espn_public: a, sleeper: b } = manifest().leagues;
    await page.goto(`/l/${b}/waivers?view=ALL`);
    await expect(page.getByRole("region", { name: "Waiver shortlist" })).toBeVisible();
    await page.goto(`/l/${a}/waivers?view=ALL`);
    const section = page.getByRole("region", { name: "Waiver shortlist" });
    await expect(section).toContainText("No saved players yet");
    await expect(section).toContainText("this device");
    const save = page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first();
    await expect(save).toBeEnabled();
    const id = Number(await save.getAttribute("data-player-id"));
    const name = (await save.getAttribute("aria-label"))!.replace(/^Save /, "").replace(/ to shortlist$/, "");
    const box = await save.boundingBox();
    expect(box && box.height >= 44 && box.width >= 44).toBe(true);
    await save.focus();
    await expect(save).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(section).toContainText(name);
    if (width === 375) await section.screenshot({ path: test.info().outputPath("shortlist-mobile.png") });
    await expect(page.locator(`table button[data-player-id="${id}"]`)).toHaveAttribute("aria-pressed", "true");
    await page.reload();
    await expect(section).toContainText(name);
    expect(await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), `fi:waiver-shortlist:v1:${a}`)).toEqual([id]);
    // Client navigation between leagues must never carry A's state into B.
    await page.locator("summary").filter({ hasText: /^Fixture League/ }).click();
    await page.getByRole("menuitem", { name: "Sleeper Fixture League" }).click();
    await page.goto(`/l/${b}/waivers?view=ALL`);
    await expect(section).toContainText("No saved players yet");
    const saveB = page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first();
    await saveB.click();
    await page.goto(`/l/${a}/waivers?view=ALL`);
    await expect(section).toContainText(name);
    await section.getByRole("button", { name: `Remove ${name} from shortlist` }).focus();
    await page.keyboard.press("Space");
    await expect(section).toContainText("No saved players yet");
    await page.reload();
    await expect(section).toContainText("No saved players yet");
    await page.goto(`/l/${b}/waivers?view=ALL`);
    await expect(section.getByRole("listitem")).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}

test("shortlist missing loaded data is unknown, removable, and independent of filters and advice", async ({ page }) => {
  const a = manifest().leagues.espn_public;
  await page.goto(`/l/${a}/waivers?view=ALL`);
  const save = page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first();
  await expect(save).toBeEnabled();
  const id = Number(await save.getAttribute("data-player-id"));
  const name = (await save.getAttribute("aria-label"))!.replace(/^Save /, "").replace(/ to shortlist$/, "");
  const top = page.getByRole("region", { name: "Top claims" });
  const advice = () => top.evaluate((element) => {
    const copy = element.cloneNode(true) as HTMLElement;
    copy.querySelectorAll("button").forEach((button) => button.remove());
    return copy.textContent;
  });
  const before = await advice();
  await save.click();
  expect(await advice()).toBe(before);
  await page.goto(`/l/${a}/waivers?view=ALL&offset=360`);
  await expect(page.getByRole("region", { name: "Waiver shortlist" })).toContainText(name);
  // Valid saved ID absent from both loaded sets: absence is not proof of availability.
  await page.evaluate(({ key, id }) => localStorage.setItem(key, JSON.stringify([id, 999999999])), { key: `fi:waiver-shortlist:v1:${a}`, id });
  await page.goto(`/l/${a}/waivers?view=ALL&pos=K`);
  const section = page.getByRole("region", { name: "Waiver shortlist" });
  // The claim plan's check names a known player once it answers (#180); until then he is the saved ID.
  await expect(section).toContainText(new RegExp(`Player #${id}|${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
  await expect(section).toContainText("Player #999999999");
  await expect(section).toContainText("Current data not loaded");
  await expect(section.getByRole("list", { name: "Claim order" })).not.toContainText(/unavailable|waiver score/i);
  await expect(page.locator("details.filter-box > summary").first()).toContainText("K");
  await section.getByRole("button", { name: "Remove Player #999999999 from shortlist" }).click();
  await expect(section).not.toContainText("Player #999999999");
});

for (const raw of ["{", '[{"full_name":"Stale player","waiver_score":10}]']) {
  test(`shortlist malformed storage recovers: ${raw}`, async ({ page }) => {
    const a = manifest().leagues.espn_public;
    await page.addInitScript(({ key, raw }) => localStorage.setItem(key, raw), { key: `fi:waiver-shortlist:v1:${a}`, raw });
    await page.goto(`/l/${a}/waivers?view=ALL`);
    await expect(page.getByRole("region", { name: "Waiver shortlist" })).toContainText("No saved players yet");
    await page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first().click();
    await expect(page.getByRole("region", { name: "Waiver shortlist" }).getByRole("listitem")).toHaveCount(1);
  });
}

for (const operation of ["getItem", "setItem"]) {
  test(`shortlist storage ${operation} failure stays usable and reports session-only state`, async ({ page }) => {
    await page.addInitScript((operation) => {
      const original = Storage.prototype[operation];
      Object.defineProperty(Storage.prototype, operation, { value: function (this: Storage, ...args: string[]) {
        if (args[0].startsWith("fi:waiver-shortlist:")) throw new DOMException("Denied", "SecurityError");
        return Reflect.apply(original, this, args);
      } });
    }, operation);
    await page.goto(`/l/${manifest().leagues.espn_public}/waivers?view=ALL`);
    const section = page.getByRole("region", { name: "Waiver shortlist" });
    await page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first().click();
    await expect(section.getByRole("listitem")).toHaveCount(1);
    await expect(section).toContainText("this visit only");
    await section.getByRole("button", { name: /^Remove/ }).click();
    await expect(section).toContainText("No saved players yet");
  });
}


test("shortlist denied localStorage getter does not crash the page", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("Denied", "SecurityError"); } });
  });
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const errors = collectErrors(page);
  await page.goto(`/l/${manifest().leagues.espn_public}/waivers?view=ALL`);
  const section = page.getByRole("region", { name: "Waiver shortlist" });
  await expect(section).toContainText("this visit only");
  await page.locator("table").getByRole("button", { name: /^Save .* to shortlist$/ }).first().click();
  await expect(section.getByRole("listitem")).toHaveCount(1);
  expect(errors).toEqual([]);
});
