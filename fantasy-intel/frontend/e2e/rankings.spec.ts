import { expect, test, type Page } from "@playwright/test";

// Player Rankings explorer against the synthetic league (scripts/build_e2e_db.py). The viewport (1440 or 375)
// comes from the Playwright project: desktop shows the table, phones (<= 720px) show one card per player.
// Selectors are roles plus the profile-link href, so the markup can change without touching this file.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const PROFILE_LINK = 'a[href^="/players?id="]';

const isMobile = () => test.info().project.name.startsWith("mobile");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

/** Visible links to a player profile in the page body (the hidden layout's links are display:none). */
const visibleProfileLinks = (page: Page) => page.locator(`main ${PROFILE_LINK}:visible`);

test("rankings render players as cards on phones and as a table on desktop", async ({ page }) => {
  await page.goto("/rankings");
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(visibleProfileLinks(page).first()).toBeVisible();
  if (isMobile()) {
    // cards: no visible table, and no profile link inside a table
    await expect(page.getByRole("table")).toHaveCount(0);
    await expect(page.locator(`main table ${PROFILE_LINK}:visible`)).toHaveCount(0);
  } else {
    await expect(page.getByRole("table")).toBeVisible();
    await expect(page.getByRole("table").locator(PROFILE_LINK).first()).toBeVisible();
  }
});

test("desktop: the table marks the active sort column with aria-sort and header links re-sort", async ({ page }) => {
  test.skip(isMobile(), "the sortable table is the desktop layout");
  await page.goto("/rankings");
  // default order is rank ascending: exactly one column is marked
  const marked = page.locator("th[aria-sort]");
  await expect(marked).toHaveCount(1);
  await expect(marked).toHaveAttribute("aria-sort", "ascending");

  await page.getByRole("columnheader", { name: /rating/i }).getByRole("link").click();
  await expect(page).toHaveURL(/sort=rating/);
  await expect(page.locator("th[aria-sort]")).toHaveCount(1);
  await expect(page.getByRole("columnheader", { name: /rating/i })).toHaveAttribute("aria-sort", /ascending|descending/);
});

test("clicking a player name opens the existing player profile", async ({ page }) => {
  await page.goto("/rankings");
  const link = visibleProfileLinks(page).first();
  const href = await link.getAttribute("href");
  expect(href).toMatch(/^\/players\?id=\d+$/);
  await link.click();
  await expect(page).toHaveURL(/\/players\?id=\d+/);
  await expect(page.locator("h1")).toHaveCount(1);
});

test("a position filter link persists in the URL and survives a reload", async ({ page }) => {
  await page.goto("/rankings");
  await page.getByRole("link", { name: "WR", exact: true }).first().click();
  await expect(page).toHaveURL(/pos=WR/);
  await page.reload();
  await expect(page).toHaveURL(/pos=WR/);
  await expect(page.locator("main")).not.toContainText(/Something went wrong|Can't reach the backend/);
  // every visible rank label on the filtered page is a WR label
  const labels = await page.locator("main").getByText(/^(T-)?(QB|RB|TE)\d+$/).count();
  expect(labels).toBe(0);
});

test("a filter change keeps other params and a deep link restores the view", async ({ page }) => {
  await page.goto("/rankings?pos=RB&horizon=ros");
  await expect(page.locator("main")).not.toContainText(/Something went wrong|Can't reach the backend/);
  await page.getByRole("link", { name: "WR", exact: true }).first().click();
  await expect(page).toHaveURL(/pos=WR/);
  await expect(page).toHaveURL(/horizon=ros/);
  await expect(page).not.toHaveURL(/offset=/);
});

test("the page does not scroll sideways", async ({ page }) => {
  await page.goto("/rankings");
  await page.waitForLoadState("networkidle");
  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client);
});
