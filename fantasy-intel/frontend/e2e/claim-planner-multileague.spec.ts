import fs from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page, type Request } from "@playwright/test";

// The Waivers claim planner (issue #180) on the multi-league site (playwright.multileague.config.ts): the shortlist is
// device-local and league-scoped, so the planner only exists under /l/<lg>/. Real scoped page and API, real browser
// storage. In the ESPN fixture league (scripts/build_e2e_db.py) team 2 has FAAB $80 of $100 left and one open active
// spot; Kilo RB (207) sits in its IR slot with an ACTIVE status, which blocks every add until he is dropped; Waiver WR
// (309) is on waivers and Waiver RB (209) is a free agent.
type Manifest = { leagues: Record<"espn_public" | "sleeper", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const planKey = (lg: string, team: number) => `fi:waiver-claim-plan:v1:${lg}:${team}`;
const shortlistKey = (lg: string) => `fi:waiver-shortlist:v1:${lg}`;

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

/** The league signal's status as the page derives it (36h, signal-provenance.v1). */
async function leagueCurrent(page: Page, lg: string): Promise<boolean> {
  const res = await page.waitForResponse((r) => r.url().includes(`/l/${lg}/api/waiver-claim-plan`) && r.ok());
  const body = await res.json();
  const league = (body.provenance?.signals ?? []).find((s: { signal: string }) => s.signal === "league");
  if (!league?.captured_at || league.stale_reason) return false;
  return (Date.now() - Date.parse(league.captured_at)) / 36e5 <= (league.stale_after_hours ?? 36);
}

const row = (section: Locator, name: string) => section.getByRole("listitem").filter({ hasText: name });

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

for (const width of [1440, 375]) {
  test(`plan claims in order with drops and bids, keep them on reload, never carry them to another league (${width}px)`, async ({ page, context }) => {
    await page.setViewportSize({ width, height: 900 });
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const { espn_public: a, sleeper: b } = manifest().leagues;
    const seen = watch(page);
    await page.goto(`/l/${a}/waivers?team=2`);
    await page.evaluate(({ key }) => localStorage.setItem(key, JSON.stringify([309, 209])), { key: shortlistKey(a) });
    const firstCheck = page.waitForResponse((r) => r.url().includes(`/l/${a}/api/waiver-claim-plan`));
    await page.reload();
    const section = page.getByRole("region", { name: "Waiver shortlist" });
    await expect(section).toContainText("Not submitted: nothing here is sent to ESPN");
    await firstCheck;
    const wr = row(section, "Waiver WR");
    const rb = row(section, "Waiver RB");
    await expect(section.getByRole("list", { name: "Claim order" }).getByRole("listitem")).toHaveCount(2);
    await expect(section).toContainText("FAAB bidding: $80 of $100 remaining");
    await expect(section).toContainText(/waiver rules? not in the synced settings/);

    // The IR block and the missing bid: never a confirmed claim.
    await expect(wr).toContainText("Kilo RB is in an IR slot");
    await expect(wr).toContainText("Not valid");
    await expect(wr).toContainText("No bid entered");
    await expect(rb).toContainText("Free agent");

    // Drop Kilo RB and bid 15 on the waiver claim.
    await wr.getByLabel("Drop").selectOption("207");
    await expect(wr).not.toContainText("Kilo RB is in an IR slot");
    const check = leagueCurrent(page, a);
    await wr.getByLabel("Bid ($)").fill("15");
    const current = await check;
    await expect(wr).not.toContainText("Not valid");
    await expect(wr).toContainText(current ? "Checks pass" : "Needs review");
    if (!current) await expect(wr).toContainText("league data");

    // Too high a bid is refused against the synced budget; a bad entry is never stored.
    await wr.getByLabel("Bid ($)").fill("90");
    await expect(wr).toContainText("more than your remaining FAAB ($80)");
    await expect(wr).toContainText("Not valid");
    await wr.getByLabel("Bid ($)").fill("1.5");
    await expect(wr).toContainText("Whole dollars only");
    await wr.getByLabel("Bid ($)").fill("15");
    await expect(wr).not.toContainText("Not valid");

    // A second claim dropping the same player is flagged on the later claim; order decides which.
    await rb.getByLabel("Drop").selectOption("207");
    await expect(rb).toContainText("Claim 1 also drops Kilo RB");
    await expect(rb).toContainText("Needs review");
    const up = rb.getByRole("button", { name: "Move Waiver RB up" });
    if (width === 375) expect((await up.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await up.click();
    await expect(section.getByRole("list", { name: "Claim order" }).getByRole("listitem").first()).toContainText("Waiver RB");
    await expect(wr).toContainText("Claim 1 also drops Kilo RB");
    await expect(rb).not.toContainText("also drops");

    // Survives a reload on this device: order, candidates, drops and the bid.
    await page.reload();
    await expect(section.getByRole("list", { name: "Claim order" }).getByRole("listitem").first()).toContainText("Waiver RB");
    await expect(wr.getByLabel("Drop")).toHaveValue("207");
    await expect(wr.getByLabel("Bid ($)")).toHaveValue("15");
    expect(await page.evaluate((k) => JSON.parse(localStorage.getItem(k)!), planKey(a, 2))).toEqual({
      v: 1, league: a, team: 2, claims: [{ add: 209, drop: 207, bid: null }, { add: 309, drop: 207, bid: 15 }],
    });

    // Export says plainly it has not been submitted, with each claim's check once it has answered.
    await expect(section).toContainText(/of 2 claims pass/);
    await section.getByRole("button", { name: "Copy plan as text" }).click();
    await expect(section).toContainText("Copied. The plan has not been submitted.");
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain("NOT SUBMITTED: nothing here has been sent to ESPN");
    expect(copied).toMatch(/1\. Add Waiver RB .* drop Kilo RB/);
    expect(copied).toMatch(/2\. Add Waiver WR .* drop Kilo RB .* bid \$15 .* Needs review/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
    if (width === 375) await section.screenshot({ path: test.info().outputPath("claim-plan-mobile.png") });

    // Another league starts empty and never receives league A's candidates, drops or bids.
    await page.goto(`/l/${b}/waivers`);
    await expect(section).toContainText("No saved players yet");
    const keys = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("fi:waiver-")).sort());
    expect(keys).toEqual([planKey(a, 2), shortlistKey(a)].sort());
    await expect(section).not.toContainText(/Waiver WR|Waiver RB|Kilo RB/);

    expect(seen.writes, "no provider calls and no non-GET API requests").toEqual([]);
    expect(seen.errors, "console errors").toEqual([]);
  });
}

test("a plan that cannot be checked is never shown as passing", async ({ page }) => {
  const { espn_public: a } = manifest().leagues;
  await page.addInitScript(({ key }) => localStorage.setItem(key, JSON.stringify([309])), { key: shortlistKey(a) });
  await page.route("**/api/waiver-claim-plan**", (r) => r.abort());
  await page.goto(`/l/${a}/waivers?team=2`);
  const section = page.getByRole("region", { name: "Waiver shortlist" });
  await expect(section.getByText("This plan has not been checked")).toBeVisible({ timeout: 20_000 });
  await expect(section).not.toContainText("Checks pass");
  await expect(section.getByRole("listitem")).toContainText("Not checked");
});

test("the check refuses an unscoped request and a forged league header", async ({ request }) => {
  const res = await request.get("/api/waiver-claim-plan?team_id=2&claims=309", { headers: { "x-fi-league": manifest().leagues.espn_public } });
  expect(res.status()).toBe(404);
});
