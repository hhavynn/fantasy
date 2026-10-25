import { expect, test, type Page } from "@playwright/test";

// Projection-source comparison (/rankings/compare) against the synthetic ESPN league (scripts/build_e2e_db.py), at the
// Playwright project's viewport (1440 or 375). That league has ESPN projections and no stored Fantasy Intel run, so
// every ESPN rank is shown, Ours is named unavailable and no gap is computed: a missing side is never a 0.
// The Sleeper case (no provider projections at all) is source-compare-sleeper.spec.ts under playwright.sleeper.config.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
const ZERO_GAP = /^\s*[▲▼]?\s*[+−-]?0(\.0)?\s*$/;

const isMobile = () => test.info().project.name.startsWith("mobile");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const table = (page: Page) => page.getByRole("table", { name: "Projection sources compared" });

test("ESPN ranks are listed, Ours is named unavailable and no gap is ever 0", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/rankings/compare");
  // wait for the real page (only it renders the table), then for the loading skeleton's h1 to be gone
  await expect(table(page).locator("tbody tr").first()).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("h1")).toHaveText("Source comparison");
  await expect(table(page).getByRole("columnheader", { name: /ESPN rank/ })).toBeVisible();
  await expect(page.getByText("Ours is unavailable here")).toBeVisible();
  // the ESPN side ranks within the position (QB1), the Ours side says why it has no rank
  await expect(table(page).locator("tbody td:nth-child(2)").first()).toHaveText(/^(T-)?(QB|RB|WR|TE)\d+$/);
  await expect(table(page).locator("tbody td:nth-child(3)").first()).toHaveText("Unavailable");
  await expect(table(page).locator("tbody td:nth-child(4)").first()).toHaveText("Ours unavailable");
  await expect(table(page).locator("tbody td:nth-child(4)").filter({ hasText: ZERO_GAP })).toHaveCount(0);
  await expect(page.getByText("Neither column is an expert consensus.").first()).toBeVisible();
  await expect(page.locator("main")).not.toContainText(/FantasyPros|consensus rank/i);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    { message: "no horizontal page scroll" }).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

test("filters live in the URL and survive a reload", async ({ page }) => {
  await page.goto("/rankings/compare");
  await expect(table(page).locator("tbody tr").first()).toBeVisible();
  await page.getByRole("navigation", { name: "Position" }).getByRole("link", { name: "WR", exact: true }).click();
  await expect(page).toHaveURL(/pos=WR/);
  if (isMobile()) await page.locator("summary", { hasText: "Filters" }).click();
  await page.getByRole("navigation", { name: "Rank scope" }).getByRole("link", { name: "All positions" }).click();
  await expect(page).toHaveURL(/scope=overall/);
  await expect(page).toHaveURL(/pos=WR/);
  // overall ranks read "#12": the rank across QB, RB, WR and TE, kept while only receivers are listed
  await expect(table(page).locator("tbody td:nth-child(2)").first()).toHaveText(/^(T-)?#\d+$/);
  await page.reload();
  // the filter summary (visible on phones while the chips are folded away) reads the state back from the URL
  await expect(page.locator("summary", { hasText: "Filters" })).toContainText("All positions");
  await expect(page.getByRole("navigation", { name: "Position" }).getByRole("link", { name: "WR", exact: true }))
    .toHaveAttribute("aria-current", "page");
  await expect(table(page).locator("tbody td:nth-child(2)").first()).toHaveText(/^(T-)?#\d+$/);
});

test("desktop: sorting by the ESPN rank marks that column", async ({ page }) => {
  test.skip(isMobile(), "header sorting is checked once, on desktop");
  await page.goto("/rankings/compare?sort=provider_rank&scope=overall");
  await expect(table(page).getByRole("columnheader", { name: /ESPN rank/ })).toHaveAttribute("aria-sort", "ascending");
  await expect(table(page).locator("tbody td:nth-child(2)").first()).toHaveText(/^(T-)?#1$/);
});

test("Rankings links to the comparison", async ({ page }) => {
  await page.goto("/rankings");
  await page.getByRole("link", { name: "Compare sources" }).click();
  await expect(page).toHaveURL(/\/rankings\/compare/);
  await expect(table(page).locator("tbody tr").first()).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("h1")).toHaveText("Source comparison");
});
