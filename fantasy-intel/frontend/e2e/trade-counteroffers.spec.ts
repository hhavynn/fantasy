import { expect, test, type Page } from "@playwright/test";
import { applyTheme, expectAccessible, expectNoSidewaysScroll } from "./ux";

// Counteroffers (#214) in the trade analyzer against the synthetic league (scripts/build_e2e_db.py): team 2 sends
// 307 "Sierra WR" for team 1's 303 "Oscar WR". The search runs only when asked for, every listed counteroffer opens in
// the builder, a kept player stays in every counteroffer, and an offer nothing improves says so.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function qualityBar(page: Page) {
  // The evaluation and the panel stream in; check the settled page (TableScroll marks wide tables a frame later).
  await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
  await expect.poll(() => page.locator("main .table-wrap").evaluateAll((els) => els.every((e) =>
    (e.scrollWidth > e.clientWidth + 1) === (e.getAttribute("tabindex") === "0"))), {
    message: "tabindex=0 on exactly the tables that overflow", timeout: 10_000 }).toBe(true);
  for (const theme of ["dark", "light"] as const) {
    await applyTheme(page, theme);
    await expectNoSidewaysScroll(page);
    await expectAccessible(page, theme);
  }
}

const panel = (page: Page) => page.locator("section", { has: page.getByRole("heading", { name: "Counteroffers" }) });

test("counteroffers are listed on request, keep locked players and open in the builder", async ({ page }) => {
  test.slow();
  await page.goto("/trades?ta=2&tb=1&a=307&b=303");
  const find = page.getByRole("link", { name: "Find counteroffers" });
  await expect(find).toBeVisible({ timeout: 30_000 });
  await qualityBar(page);
  await find.click();
  await expect(page).toHaveURL(/[?&]co=1/);
  const co = panel(page);
  await expect(co.getByText(/counteroffers? helps? the side this offer treats worse/)).toBeVisible({ timeout: 30_000 });
  const items = co.locator("ol > li");
  expect(await items.count()).toBeGreaterThan(0);
  expect(await items.count()).toBeLessThanOrEqual(3);
  const text = (await co.innerText()).toLowerCase();
  for (const word of ["guarantee", "will accept", "would accept", "likely"]) expect(text, word).not.toContain(word);
  await qualityBar(page);

  await co.getByRole("link", { name: "Sierra WR" }).click();
  // The lock link re-renders the counteroffers on the server, which can take longer than the 5s default.
  await expect(page).toHaveURL(/[?&]lock=307/, { timeout: 30_000 });
  await expect(co.getByRole("link", { name: "Kept: Sierra WR" })).toBeVisible({ timeout: 30_000 });
  await expect(co.getByText(/instead of Sierra WR|Team 2 keeps Sierra WR/)).toHaveCount(0);

  const open = co.getByRole("link", { name: "Open in the builder" }).first();
  const href = await open.getAttribute("href");
  const want = new URL(href!, "http://x").searchParams;
  expect(want.getAll("a")).toContain("307");
  expect(want.get("co")).toBeNull();
  await open.click();
  await expect(page).toHaveURL(/\/trades\?/);
  await expect(page.getByRole("link", { name: "Find counteroffers" })).toBeVisible({ timeout: 30_000 });
});

test("an offer no edit improves explains why there is no counteroffer", async ({ page }) => {
  test.slow();
  await page.goto("/trades?ta=2&tb=1&a=304&b=303&co=1");
  await expect(panel(page).getByText("No counteroffer within these limits does better")).toBeVisible({ timeout: 30_000 });
  await expect(panel(page).getByText(/packages? evaluated exactly/)).toBeVisible();
});
