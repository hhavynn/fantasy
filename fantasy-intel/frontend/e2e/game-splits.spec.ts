import { expect, test, type Page } from "@playwright/test";

// Game splits (issue #210) against the synthetic SPL world in the E2E database (backend/tests/synthetic_splits.py):
// a receiver with and without a teammate who was inactive, on the reserve list, partial and unknown in different weeks.
// Every assertion waits on rendered data, not on the loading skeleton.
const SPLIT = "/research/splits?player=00-SPLWR01&teammate=00-SPLWR02&season_from=2025";

async function noHorizontalScroll(page: Page) {
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
}

test("a full split shows separate samples, unknown status excluded, and a reproducible week list", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(SPLIT);
  await expect(page.getByRole("heading", { level: 1, name: "Game splits" })).toBeVisible();
  const summary = page.getByRole("heading", { name: /Split Receiver.*with and without Split Wingman/ });
  await expect(summary).toBeVisible();
  await expect(page.getByText("With Split Wingman", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: /Usage and production/ })).toContainText("xFP/G");
  await expect(page.getByRole("table", { name: /Team environment/ })).toContainText("Split Starter");
  await expect(page.getByText("Not a causal estimate", { exact: true })).toBeVisible();

  const weeks = page.getByRole("table", { name: "Weekly source rows for the split" });
  await expect(weeks.locator('tr[data-outcome="with"]')).toHaveCount(5);
  await expect(weeks.locator('tr[data-outcome="without"]')).toHaveCount(4);
  await expect(weeks.locator('tr[data-outcome="excluded"]')).toHaveCount(2);
  await expect(weeks).toContainText("Bye");

  await page.getByRole("link", { name: /^Excluded/ }).click();
  await expect(page).toHaveURL(/show=excluded/);
  await expect(page).toHaveURL(/teammate=00-SPLWR02/);
  await expect(weeks.locator("tbody tr")).toHaveCount(2);
  await expect(weeks).toContainText("Partial game");
  await expect(weeks).toContainText("Active, but no box-score or snap record");
  await noHorizontalScroll(page);
  expect(errors).toEqual([]);
});

test("too few games withholds the differences and says why", async ({ page }) => {
  await page.goto(`${SPLIT}&as_of=2025-10-01`);
  await expect(page.getByText("Too few games to compare")).toBeVisible();
  await expect(page.getByText(/each group needs at least 3 games \(with: 4, without: 0\)/)).toBeVisible();
  await expect(page.getByRole("table", { name: "Weekly source rows for the split" })).toContainText("On or after the cutoff date");
  await noHorizontalScroll(page);
});

test("the form picks a player, then a teammate, and keeps the window in the URL", async ({ page }) => {
  await page.goto("/research/splits?season_from=2025");
  await expect(page.getByText("Choose a player", { exact: true })).toBeVisible();
  await page.locator("select[name=player]").selectOption("00-SPLWR01");
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(/player=00-SPLWR01/);
  await expect(page.getByRole("heading", { name: /Teammates of Split Receiver/ })).toBeVisible();
  await page.locator("select[name=teammate]").selectOption("00-SPLQB02");
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(/teammate=00-SPLQB02/);
  await expect(page).toHaveURL(/season_from=2025/);
  await expect(page.getByRole("heading", { name: /with and without Split Backup/ })).toBeVisible();
  await expect(page.getByText(/More than one quarterback started/)).toBeVisible();
  await noHorizontalScroll(page);
});
