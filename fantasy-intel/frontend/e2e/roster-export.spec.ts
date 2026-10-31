import { expect, test } from "@playwright/test";
import { open, stubPhotos } from "./ux";

/** Fixture-only copy workflow. No user data, account, provider credential, or public sharing route. */
test.beforeEach(async ({ page, context }) => {
  await stubPhotos(page);
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: "http://127.0.0.1:3100",
  });
});

test("authorized Team Value can copy only a name-free local roster summary", async ({ page }) => {
  await open(page, "/my-team/value");
  const copy = page.getByRole("button", { name: "Copy name-free roster summary", exact: true });
  await expect(copy).toBeVisible();
  await copy.click();
  await expect(page.getByRole("status").filter({ hasText: "Name-free roster summary copied." })).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("Fantasy Intel · name-free roster outlook");
  expect(copied).toMatch(/Perspective: selected authorized roster/);
  expect(copied).toMatch(/League snapshot captured: (unknown|\d{4}-\d\d-\d\dT)/);
  expect(copied).toMatch(/Projection snapshot captured: (unknown|\d{4}-\d\d-\d\dT)/);
  expect(copied).not.toMatch(/https?:\/\/|league_id|espn_player_id|PRIVATE_|\[object Object\]|\bNaN\b/);
  await expect(page.getByRole("link", { name: /create public share link/i })).toHaveCount(0);
});

test("the exported window follows the selected phase instead of reusing a stale summary", async ({ page }) => {
  await open(page, "/my-team/value?phase=playoff");
  await page.getByRole("button", { name: "Copy name-free roster summary", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Name-free roster summary copied." })).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("Window: Playoffs");
});
