import { expect, test, type Page } from "@playwright/test";

// Opportunity tree on /injury-impact (issue #190) against the synthetic league (scripts/build_e2e_db.py). The fixture
// league has no injured or returning starter with a real role and no depth chart, so the tree must say so in words:
// no team is listed, nobody is named as a beneficiary, the missing depth charts are a visible notice, and the route
// source is "unavailable" rather than named. The populated tree (observed beneficiaries, hypotheses, abstentions,
// ownership) is covered by backend/tests/test_opportunity_tree.py and lib/opportunity-tree.test.ts. Runs on
// desktop-1440 and mobile-375.

// loading.tsx streams the same h1 first, and the tree streams in its own Suspense boundary after the injury list, so
// "loaded" is the tree's own content, never the heading or a skeleton.
const tree = (page: Page) => page.getByTestId("opportunity-tree");

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText("Injury impact", { timeout: 20_000 });
  await expect(tree(page), "opportunity tree still streaming").toBeVisible({ timeout: 30_000 });
}

test("with no injured or returning starter the tree names nobody and says why", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/injury-impact");
  await loaded(page);
  const main = page.locator("main");
  await expect(main.getByRole("heading", { name: "Depth charts and opportunity" })).toBeVisible();
  await expect(tree(page).getByText(/^No team has an injured or returning starter with a real role in week \d+\.$/)).toBeVisible();
  // No depth chart in the fixture: a visible notice, never a silent ordering.
  await expect(tree(page).getByText("No depth charts synced")).toBeVisible();
  await expect(tree(page).getByText(/groups are ordered by recent snap share, and no depth-chart hypothesis is made/)).toBeVisible();
  // No route provider supplied values, so none is named.
  await expect(tree(page).getByText(/Route participation: unavailable\./)).toBeVisible();
  await expect(tree(page).locator("details")).toHaveCount(0);
  await expect(tree(page).getByRole("link", { name: "Check claim" })).toHaveCount(0);
  const text = (await tree(page).innerText()).toLowerCase();
  for (const word of ["probability", "confirmed starter", "fantasypros", "no claim needed"]) expect(text, word).not.toContain(word);
  const [scroll, width] = await page.evaluate(() => [document.scrollingElement!.scrollWidth, window.innerWidth]);
  expect(scroll, "no horizontal page scroll").toBeLessThanOrEqual(width);
  expect(errors).toEqual([]);
});

test("the tree keeps the analyzed team in the request", async ({ page }) => {
  await page.goto("/injury-impact?team=3");
  await loaded(page);
  await expect(tree(page).getByText(/^No team has an injured or returning starter/)).toBeVisible();
  await expect(page.locator("main")).not.toContainText("could not be loaded");
});
