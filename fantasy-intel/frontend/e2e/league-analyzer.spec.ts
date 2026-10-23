import { expect, test, type Page } from "@playwright/test";

// League analyzer (issue #191) against the synthetic league (scripts/build_e2e_db.py: team 2 is the owner default, team 1
// a rival). The matrix, the position filter and the compare-two-teams mode at 320, 390, 768 and 1440 px: no sideways page
// scroll, every cell either ranked ("Nth of M") or unknown in words, links into scouting, trade discovery and team
// value, and no leaked engine values. The widths are set inside the tests, so they run on the desktop-1440 project only.
const WIDTHS = [320, 390, 768, 1440];
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1440", "each test sets its own widths");
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

// loading.tsx streams the same h1 and a skeleton table first, so "loaded" is the matrix itself (its Power column and a
// team row), never the heading.
const matrix = (page: Page) => page.getByRole("table").filter({ has: page.getByRole("columnheader", { name: "Power" }) });

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading").toHaveText("League analyzer", { timeout: 20_000 });
  await expect(matrix(page).locator("tbody tr").first(), "matrix still streaming").toBeVisible({ timeout: 30_000 });
}

async function noSideScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [document.scrollingElement!.scrollWidth, window.innerWidth]);
  expect(scroll, "no horizontal page scroll").toBeLessThanOrEqual(width);
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  return errors;
}

for (const width of WIDTHS) {
  test(`the matrix and the compare mode at ${width}px`, async ({ page }) => {
    const errors = collectErrors(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/league-analyzer");
    await loaded(page);
    const main = page.locator("main");
    await expect(main.locator('[class*="verdict"]').first(), "the answer comes first").toBeVisible();
    await expect(main.locator("h1")).toHaveCount(1);
    const rows = matrix(page).locator("tbody tr");
    expect(await rows.count()).toBeGreaterThan(2);
    // every position cell is either ranked within the league or unknown in words: never a bare number grade
    const cells = await rows.first().locator("td.num").allInnerTexts();
    for (const c of cells.slice(1)) expect(c, "cell").toMatch(/of \d+|Unknown|Unverified|—/);
    const text = await main.innerText();
    for (const bad of ["NaN", "undefined", "win probability", "Ours projections"]) expect(text, bad).not.toContain(bad);
    await expect(main.getByText(/ESPN projections/).first()).toBeVisible();
    await noSideScroll(page);

    // each row links into manager scouting, trade discovery and team value, inside this league
    const rival = rows.filter({ hasText: "Team 1" }).first();
    await expect(rival.getByRole("link", { name: "Scouting" })).toHaveAttribute("href", "/managers/1");
    await expect(rival.getByRole("link", { name: "Trades" })).toHaveAttribute("href", "/trades/discover?team=2&opponent=1");
    await expect(rival.getByRole("link", { name: "Value" })).toHaveAttribute("href", "/my-team/value?team=1");

    // compare two teams: starters and bench by position, bye conflicts and needs, shareable in the URL
    await rival.getByRole("link", { name: "Compare" }).click();
    await expect(page).toHaveURL(/\/league-analyzer\?team=2&vs=1$/);
    await expect(main.getByRole("heading", { level: 2, name: / vs / })).toBeVisible({ timeout: 30_000 });
    await expect(main.getByRole("heading", { name: /bye conflicts/ })).toHaveCount(2);
    await expect(main.getByRole("heading", { name: "Positional needs" })).toBeVisible();
    await expect(main.getByRole("link", { name: "trades between these teams" })).toHaveAttribute("href", "/trades/discover?team=2&opponent=1");
    await noSideScroll(page);
    expect(errors, "console errors").toEqual([]);
  });
}

test("filters live in the URL: one position in detail, this week, and the compare form", async ({ page }) => {
  const errors = collectErrors(page);
  await page.goto("/league-analyzer");
  await loaded(page);
  const main = page.locator("main");
  await main.getByRole("navigation", { name: "Position" }).getByRole("link", { name: "RB", exact: true }).click();
  await expect(page).toHaveURL(/\/league-analyzer\?pos=RB$/);
  await expect(matrix(page).getByRole("columnheader", { name: "RB over repl." })).toBeVisible();
  await expect(matrix(page).getByRole("columnheader", { name: "Vs median" })).toBeVisible();
  await main.getByRole("navigation", { name: "Horizon" }).getByRole("link", { name: "This week" }).click();
  await expect(page).toHaveURL(/horizon=week/);
  await expect(page).toHaveURL(/pos=RB/);
  await expect(main.getByText(/This week · week \d+/).first()).toBeVisible();

  const form = main.getByRole("form", { name: "Compare with" });
  await form.getByRole("combobox").selectOption({ label: "Team 1" });
  await form.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(/vs=1/);
  await expect(page).toHaveURL(/horizon=week/);
  await expect(main.getByRole("heading", { level: 2, name: / vs / })).toBeVisible({ timeout: 30_000 });
  await main.getByRole("link", { name: "Close comparison" }).click();
  await expect(page).not.toHaveURL(/vs=/);
  expect(errors, "console errors").toEqual([]);
});

test("a team that is not in the league says so instead of comparing", async ({ page }) => {
  await page.goto("/league-analyzer?vs=99");
  await expect(page.locator("main h1:visible")).toHaveText("League analyzer", { timeout: 20_000 });
  await expect(page.locator("main").getByText(/isn't a team in this league/)).toBeVisible({ timeout: 30_000 });
});
