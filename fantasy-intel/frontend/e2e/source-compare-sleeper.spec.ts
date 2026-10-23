import { expect, test } from "@playwright/test";

// Projection-source comparison in a Sleeper league (scripts/build_e2e_sleeper.py, playwright.sleeper.config.ts, 1440 and
// 375): Sleeper publishes no projections, so the provider side is "Provider", every provider rank says it is not
// published (never 0, never a rank) and ESPN is named nowhere. Fantasy Intel's own ranks are listed.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test("sleeper: no provider ranks, never zero, never ESPN; Ours is ranked", async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/rankings/compare");
  const table = page.getByRole("table", { name: "Projection sources compared" });
  await expect(table.locator("tbody tr").first()).toBeVisible();
  await expect(table.getByRole("columnheader", { name: /^Provider rank/ })).toBeVisible();
  await expect(page.getByText("Provider rankings are unavailable here")).toBeVisible();
  const providerCells = table.locator("tbody td:nth-child(2)");
  await expect(providerCells.first()).toHaveText("Not published");
  await expect(providerCells.filter({ hasNotText: "Not published" })).toHaveCount(0);
  await expect(table.locator("tbody td:nth-child(3)").filter({ hasText: /^(T-)?(QB|RB|WR|TE)\d+$/ }).first()).toBeVisible();
  await expect(table.locator("tbody td:nth-child(4)").filter({ hasNotText: "Sleeper publishes none" })).toHaveCount(0);
  const lines = (await page.locator("body").innerText()).split("\n").filter((l) => /\bESPN\b/.test(l));
  expect(lines, "no ESPN wording on a Sleeper league's page").toEqual([]);
  const width = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(width, "no horizontal page scroll").toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});
