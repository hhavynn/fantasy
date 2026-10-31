import { expect, test } from "@playwright/test";
import { open, stubPhotos } from "./ux";

test.beforeEach(async ({ page }) => { await stubPhotos(page); });

test("stash planner discloses conditional evidence coverage without predicting injuries", async ({ page }) => {
  await open(page, "/waivers");
  const heading = page.getByRole("heading", { name: "Conditional opportunities" });
  await expect(heading).toBeVisible();
  // The synthetic fixture has no depth charts and no supported conditional scenarios.
  await expect(page.locator("main")).toContainText("No evidence-backed conditional opportunities were identified in this snapshot.");
  await page.getByText("Evidence coverage and limits").click();
  await expect(page.locator("main")).toContainText("No depth charts are synced");
  await expect(page.locator("main")).toContainText("not injury probabilities");
});
