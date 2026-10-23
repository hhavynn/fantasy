import { expect, test, type Page } from "@playwright/test";

// The bye and lineup-coverage planner on Schedule intelligence (issue #177) against the synthetic league
// (scripts/build_e2e_db.py). Team 3 rosters one QB, RB, WR and TE and no K or D/ST, so every planned week is short
// (the first short slot is RB); team 2, the owner default, covers every slot. Nothing in the fixture has kicked off,
// so the plan starts at week 3. Everything below is driven from the keyboard: the matrix on desktop-1440 and the week
// grid on mobile-375.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const isPhone = () => /^mobile/.test(test.info().project.name);

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const planner = (page: Page) => page.locator("#bye-planner");

async function open(page: Page, query: string) {
  await page.goto(`/schedule-intelligence${query}`);
  await expect(planner(page).getByRole("heading", { name: "Lineup coverage by week" })).toBeVisible({ timeout: 20_000 });
}

/** Start keyboard focus at the planner's heading, then Tab (at most `max` times) until the focused element matches. */
async function tabTo(page: Page, matches: () => boolean, max = 40): Promise<void> {
  await page.evaluate(() => {
    const h = document.querySelector<HTMLElement>("#bye-planner h2")!;
    h.tabIndex = -1;
    h.focus();
  });
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    if (await page.evaluate(matches)) return;
  }
  throw new Error("element never received keyboard focus");
}

async function focusedId(page: Page) {
  return page.evaluate(() => document.activeElement?.id ?? null);
}

test("a conflict week is reached, opened and acted on from the keyboard", async ({ page }) => {
  await open(page, "?team=3");
  await expect(page.getByTestId("bye-planner-summary")).toContainText("Week 3: 5 starting slots cannot be filled");
  const matrix = page.getByTestId("bye-matrix");
  const grid = page.getByTestId("bye-grid");
  if (isPhone()) {
    await expect(matrix).toBeHidden();
    await expect(grid).toBeVisible();
    await tabTo(page, () => document.activeElement?.getAttribute("href") === "#bye-week-3");
    await expect(page.locator(":focus")).toHaveText(/Details/);
  } else {
    await expect(matrix).toBeVisible();
    await expect(grid).toBeHidden();
    await tabTo(page, () => document.activeElement?.getAttribute("href") === "#bye-week-3");
    await expect(page.locator(":focus")).toHaveText(/Wk 3/);
  }
  // Enter follows the week link and moves focus to that week's conflict detail.
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#bye-week-3$/);
  await expect.poll(() => focusedId(page)).toBe("bye-week-3");
  const detail = page.getByTestId("bye-week-3");
  await expect(detail).toContainText("cannot be filled from the roster (RB, WR, K, D/ST, FLEX)");
  // The next Tab stop is the detail's Start/Sit link, in the same team and week.
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveText("Open Start/Sit for week 3");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveText("Find waiver options for week 3");
  await expect(page.locator(":focus")).toHaveAttribute("href", "/waivers?pos=RB&week=3&team=3");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/start-sit\?week=3&team=3$/, { timeout: 20_000 });
});

test("the waivers link opens Waivers for the same team, naming the planned week", async ({ page }) => {
  await open(page, "?team=3");
  await tabTo(page, () => document.activeElement?.textContent === "Find waiver options for week 4", 200);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/waivers\?pos=RB&week=4&team=3$/, { timeout: 20_000 });
  await expect(page.getByText("Looking for week 4 cover")).toBeVisible();
});

test("a fully covered roster reports no conflicts and offers no actions", async ({ page }) => {
  await open(page, "");
  await expect(page.getByTestId("bye-planner-summary")).toContainText("No conflicts: every planned week is covered");
  await expect(planner(page).locator('a[href^="#bye-week-"]')).toHaveCount(0);
  await expect(planner(page).locator('a[href*="start-sit"]')).toHaveCount(0);
});
