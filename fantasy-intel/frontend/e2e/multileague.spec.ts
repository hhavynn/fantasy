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

// Owner-only research pages need a platform admin: for these visitors they answer "not found", checked separately.
const OWNER_ONLY = new Set(["/research", "/projection-performance"]);
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
