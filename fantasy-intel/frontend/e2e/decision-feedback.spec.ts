import { expect, test, type Page } from "@playwright/test";

// "Your decisions" on the weekly recap (issue #194) against the synthetic league (scripts/build_e2e_db.py). The fixture
// stores its roster snapshot after the played weeks kicked off, so no week has an archive taken before kickoff: the
// section must say so in words, grade nothing and show no number for a call, while the recap around it stays complete.
// Waits on real content (never a loading skeleton). Runs on desktop-1440 and mobile-375.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const feedback = (page: Page) => page.getByTestId("decision-feedback");

async function open(page: Page, query = "") {
  await page.goto(`/weekly-recap${query}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("recap", { timeout: 20_000 });
  await expect(feedback(page).getByRole("heading", { name: "Your decisions" })).toBeVisible({ timeout: 20_000 });
}

test("a week without an archive taken before kickoff says so and grades nothing", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await open(page);
  const f = feedback(page);
  await expect(f.getByText("No archived recommendations for this week").first()).toBeVisible();
  await expect(f.getByText(/No roster snapshot of your team was archived/).first()).toBeVisible();
  await expect(f.locator("[aria-busy=true]")).toHaveCount(0);                 // real content, not the skeleton
  await expect(f.getByRole("table").filter({ hasText: "Points lost" }).locator("tbody tr")).toHaveCount(2);   // season: weeks 1-2
  await expect(f.getByText("Calls this week")).toHaveCount(0);                 // nothing graded, no call table
  await expect(f).not.toContainText(/probabilit|confidence/i);
  // the recap itself is intact beside it
  await expect(page.getByRole("heading", { name: "Scoreboard" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /^Standings after/ })).toBeVisible();
  // how it is graded is one disclosure away
  await f.getByText("How decision feedback is graded").click();
  await expect(f.getByText(/correct \/ \(correct \+ missed\)/)).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

test("the forecast source is a URL-backed switch that keeps the week", async ({ page }) => {
  await open(page, "?week=1");
  const f = feedback(page);
  const tabs = f.getByRole("navigation", { name: "Forecast source" });
  await expect(tabs.getByRole("link", { name: "ESPN" })).toHaveAttribute("aria-current", "page");
  await tabs.getByRole("link", { name: "Ours" }).click();
  await expect(page).toHaveURL(/week=1/);
  await expect(page).toHaveURL(/source=independent/);
  await expect(f.getByText(/Ours forecasts/).first()).toBeVisible({ timeout: 20_000 });
  await expect(f.getByRole("navigation", { name: "Forecast source" }).getByRole("link", { name: "Ours" }))
    .toHaveAttribute("aria-current", "page");
  await expect(f.getByText("No archived recommendations for this week").first()).toBeVisible();
});
