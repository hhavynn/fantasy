import { expect, test, type Page } from "@playwright/test";

// "Why it changed" on /movers (issue #225; playwright.attribution.config.ts, 1440 and 375). The fixture
// (scripts/build_e2e_attribution.py) is the Sleeper league, whose movers are Fantasy Intel's own, with two stored forecast
// cycles of the same model artifact: Foxtrot RB was withheld (reserve list) in the earlier forecast; every other
// player's points moved through two stored input terms plus one the earlier forecast did not store (the residual).
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

async function open(page: Page, path: string) {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  await page.goto(path);
  // real content, not the loading skeleton: the detail section's heading and the split's own heading
  await expect(page.getByRole("heading", { name: "Player detail" })).toBeVisible();
  const why = page.locator("#why");
  await expect(why.getByRole("heading", { name: "Why it changed" })).toBeVisible();
  return why;
}

async function noHorizontalScroll(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    { message: "no horizontal page scroll" }).toBeLessThanOrEqual(0);
}

test("a status change is the whole move and cites only the stored reason", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const why = await open(page, "/movers?player=202");
  await expect(page.locator("h1")).toHaveCount(1);
  const rows = why.getByRole("list", { name: /Parts of the change for Foxtrot RB/ });
  const status = rows.locator("details").filter({ hasText: "Status withheld (unavailable) → projected" });
  await expect(status.locator("summary")).toContainText("Stored status");
  await status.locator("summary").click();
  await expect(status).toContainText(
    "Earlier forecast: not on the NFL active roster (weekly roster status RES, week 1) (stored with the forecast)");
  await expect(why).toContainText("The parts below account for the whole change");
  await expect(why).toContainText(/Parts and residual add up to the change of \+\d+\.\d\d points/);
  await expect(why).toContainText(/Earlier forecast made Sep 1 12:00 UTC, games through 2026 week 1\. Latest forecast made Sep 21 12:00 UTC, games through 2026 week 2\./);
  await noHorizontalScroll(page);
  expect(errors).toEqual([]);
});

test("input rows open to their values and weeks, and the residual says what is not stored", async ({ page }) => {
  const why = await open(page, "/movers?player=303");
  const rows = why.getByRole("list", { name: /Parts of the change for Oscar WR/ });
  const targets = rows.locator("details").filter({ hasText: "targets per game" });
  await expect(targets.locator("summary")).toContainText("Measured input");
  await expect(targets.locator("summary")).toContainText(/[+−]\d+\.\d\d/);
  await targets.locator("summary").click();
  await expect(targets).toContainText("Earlier 6 → latest 7");
  await expect(targets).toContainText("weeks 3–17");
  const snap = rows.locator("details").filter({ hasText: "offensive snap share" });
  await snap.locator("summary").click();
  await expect(snap).toContainText("Earlier 68% → latest 72%");
  const residual = rows.locator("details").filter({ hasText: "Residual" });
  await expect(residual.locator("summary")).toContainText("Not explained");
  await residual.locator("summary").click();
  await expect(residual).toContainText("Inputs stored in only one of the two forecasts");
  await why.getByText(/Inputs stored without a measured share/).click();
  await expect(why).toContainText("opponent points allowed to position vs league");
  // missing is a muted dash with its reason as the title, never a 0
  await expect(why.locator('.missing[title="not among the inputs this forecast stored"]').first()).toBeVisible();
  await expect(why).toContainText("Source: Fantasy Intel model.");
  await noHorizontalScroll(page);
});

test("a quarterback's input shares are labelled approximate", async ({ page }) => {
  const why = await open(page, "/movers?player=101");
  await expect(why.locator("summary").filter({ hasText: "targets per game" })).toContainText("Input (approximate)");
  await expect(why).toContainText("Quarterback forecasts blend a start and a no-start prediction");
});

test("the player profile's Outlook tab shows the same split and opens it on the movers page", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  await page.goto("/players?id=303&tab=outlook");
  const section = page.getByRole("region", { name: "Fantasy Intel forecast change" });
  await expect(section).toBeVisible();
  const why = section.locator("#why");
  await expect(why.getByRole("heading", { name: "Why it changed" })).toBeVisible();
  await expect(why.getByRole("list", { name: /Parts of the change for Oscar WR/ })).toBeVisible();
  await expect(why).toContainText(/Parts and residual add up to the change of [+−]\d+\.\d\d points/);
  await expect(section).toContainText(/Projected points summed over weeks [\d–, ]+: \d+\.\d → \d+\.\d \([+−]\d+\.\d pts\)/);
  await section.getByRole("link", { name: "Open in Projection movers" }).click();
  await expect(page).toHaveURL(/\/movers\?source=independent&player=303$/);
  await expect(page.getByRole("heading", { name: "Player detail" })).toBeVisible();
  await noHorizontalScroll(page);
  expect(errors).toEqual([]);
});
