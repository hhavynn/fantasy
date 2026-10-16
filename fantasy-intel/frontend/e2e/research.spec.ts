import { expect, test } from "@playwright/test";

// Research lab interactions against the synthetic pool (backend/tests/synthetic_research.py).
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

test("keyboard selection of a chart marker opens the evidence panel and keeps filters in the URL", async ({ page }) => {
  await page.goto("/research/wr?window=last5");
  const marker = page.locator('svg circle[role="button"]').first();
  await marker.focus();
  await expect(marker).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/window=last5/);
  await expect(page).toHaveURL(/sel=00-SYNWR/);
  await expect(page.locator("#evidence")).toContainText("xFP/G");
  await expect(page.locator("#evidence")).toContainText("Opportunity trend");
});

test("leaderboard sorting keeps the view and table rows link to selection", async ({ page }) => {
  await page.goto("/research/rb?view=workload");
  await page.getByRole("link", { name: /Backfield %/ }).click();
  await expect(page).toHaveURL(/sort=backfield_rush_share/);
  await expect(page).toHaveURL(/view=workload/);
  await page.locator("table a.player-link").first().click();
  await expect(page).toHaveURL(/sel=00-SYNRB/);
  await expect(page.getByRole("heading", { name: /week by week/ })).toBeVisible();
});

test("the timeline separates missing xFP from a measured value", async ({ page }) => {
  // Synthetic RB 6 has no xFP in week 7 (ffopportunity not published): excluded, never shown as 0.
  await page.goto("/research/rb?sel=00-SYNRB06");
  await expect(page.getByText("Played; xFP not published")).toBeVisible();
  await expect(page.locator("#evidence")).toContainText("Coverage");
});

test("filters apply through the form", async ({ page }) => {
  await page.goto("/research/te");
  await page.locator("select[name=window]").selectOption("last3");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/window=last3/);
  await expect(page.locator("main")).toContainText("(last 3 games)");
});
