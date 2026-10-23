import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { applyTheme, expectNoSidewaysScroll } from "./ux";

// Roster utility (#216) on My Team Value against the synthetic league (scripts/build_e2e_db.py; team 2 is the owner
// default). The question is a plain GET form; several players are removed together, never added up; the answer names
// who fills in, week by week, and labels its source and horizon.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const section = (page: Page) => page.locator("section", { has: page.getByRole("heading", { name: "Roster utility" }) });

async function qualityBar(page: Page) {
  await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
  await expect.poll(() => page.locator("main .table-wrap").evaluateAll((els) => els.every((e) =>
    (e.scrollWidth > e.clientWidth + 1) === (e.getAttribute("tabindex") === "0"))), {
    message: "tabindex=0 on exactly the tables that overflow", timeout: 10_000 }).toBe(true);
  for (const theme of ["dark", "light"] as const) {
    await applyTheme(page, theme);
    await expectNoSidewaysScroll(page);
    // axe on this section only: the rest of the page is covered (and triaged) by its own route checks.
    const { violations } = await new AxeBuilder({ page }).include("#roster-utility")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
    expect(violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`), theme).toEqual([]);
  }
}

test("two players removed together: one lineup, the path week by week, never a sum", async ({ page }) => {
  test.slow();
  await page.goto("/my-team/value");
  const s = section(page);
  await expect(s).toBeVisible({ timeout: 30_000 });
  const boxes = s.getByRole("checkbox");
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await s.getByRole("button", { name: "Show the lineup change" }).click();
  await expect(page).toHaveURL(/[?&]mu=-?\d+.*mu=-?\d+/, { timeout: 30_000 });
  await expect(s.getByText(/^Without .+ and .+, from your bench$/)).toBeVisible({ timeout: 30_000 });
  await expect(s.getByText(/^One at a time:/)).toBeVisible();
  const rows = s.locator("tbody tr");
  expect(await rows.count()).toBeGreaterThan(0);
  await expect(s.getByText(/Projections: /)).toBeVisible();
  const text = (await s.innerText()).toLowerCase();
  for (const word of ["trade value", "guarantee", "probability"]) expect(text, word).not.toContain(word);
  await qualityBar(page);
  // A free-agent replacement is one player at a time: asked for two, the page answers from the bench and says so.
  const url = new URL(page.url());
  url.searchParams.set("pool", "waiver");
  await page.goto(url.toString());
  await expect(s.getByText(/one player at a time, so this answer uses your bench/)).toBeVisible({ timeout: 30_000 });
});

test("one player with the waiver pool names the free agent or says none helps", async ({ page }) => {
  test.slow();
  await page.goto("/my-team/value");
  const s = section(page);
  await s.getByRole("checkbox").first().check();
  await s.getByRole("radio", { name: /best available free agent/ }).check();
  await s.getByRole("button", { name: "Show the lineup change" }).click();
  await expect(page).toHaveURL(/[?&]pool=waiver/, { timeout: 30_000 });
  await expect(s.getByText(/adding the best available free agent$/)).toBeVisible({ timeout: 30_000 });
  await expect(s.getByText("Free agent added")).toBeVisible();
  await qualityBar(page);
});
