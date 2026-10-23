import { expect, test, type Page } from "@playwright/test";

// Targeted trade discovery (issue #182) against the synthetic league (scripts/build_e2e_db.py): team 2 is the owner
// default; 201 "Echo RB" is on team 1. A goal (position, named player) narrows the trades, every row states its
// trade-offs, and "Analyze" opens exactly that trade in the analyzer. Runs on desktop-1440 (table) and mobile-375 (cards).
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText(/\S/, { timeout: 20_000 });
}

const analyzeLinks = (page: Page) => page.locator("main a:visible", { hasText: "Analyze →" });

test("a position goal narrows the trades, states trade-offs and opens the same trade in the analyzer", async ({ page }) => {
  await page.goto("/trades/discover?mode=targeted&team=2&pos=RB");
  await loaded(page);
  await expect(page.getByRole("link", { name: "Targeted search" }).first()).toHaveAttribute("aria-current", "page");
  const goal = page.getByRole("form", { name: "What you are looking for" });
  await expect(goal.getByLabel("Position to get")).toHaveValue("RB");
  await expect(page.getByText("Trades in which you receive at least one RB.")).toBeVisible({ timeout: 30_000 });

  const links = analyzeLinks(page);
  await expect(links.first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("main ul[aria-label='Trade-offs']:visible").first()).toBeVisible();
  const body = (await page.locator("main").innerText()).toLowerCase();
  for (const word of ["guarantee", "will accept", "would accept"]) expect(body, word).not.toContain(word);

  const href = await links.first().getAttribute("href");
  expect(href).toMatch(/^\/trades\?/);
  const want = new URL(href!, "http://x").searchParams;
  expect(want.get("ta")).toBe("2");
  await links.first().click();
  await expect(page).toHaveURL(/\/trades\?/);
  const got = new URL(page.url()).searchParams;
  for (const k of ["ta", "tb"]) expect(got.get(k), k).toBe(want.get(k));
  expect(got.getAll("a")).toEqual(want.getAll("a"));
  expect(got.getAll("b")).toEqual(want.getAll("b"));
  await loaded(page);
  await expect(page.getByRole("heading", { name: "Trade analyzer" })).toBeVisible();
});

test("choosing a named player in the goal form keeps only trades that bring him in", async ({ page }) => {
  await page.goto("/trades/discover?mode=targeted&team=2");
  await loaded(page);
  const goal = page.getByRole("form", { name: "What you are looking for" });
  await goal.getByLabel("Player to get").selectOption({ label: "Echo RB · RB" });
  await goal.getByRole("button", { name: "Find trades" }).click();
  await expect(page).toHaveURL(/target=201/);
  await expect(page.getByText(/Trades in which you receive Echo RB \(RB, Team 1\)/)).toBeVisible({ timeout: 30_000 });
  const links = analyzeLinks(page);
  await expect(links.first()).toBeVisible({ timeout: 30_000 });
  for (const href of await links.evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? ""))) {
    const p = new URL(href, "http://x").searchParams;
    expect(p.get("tb"), href).toBe("1");
    expect(p.getAll("b"), href).toContain("201");
  }
  // The other lenses open on their whole search: the goal does not follow a tab change.
  await expect(page.getByRole("link", { name: "Best mutual upgrades" }).first()).not.toHaveAttribute("href", /target=/);
});
