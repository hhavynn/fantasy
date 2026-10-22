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
