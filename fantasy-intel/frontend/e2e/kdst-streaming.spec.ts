import { expect, test, type Page } from "@playwright/test";

// The K and D/ST streaming plan on Schedule intelligence (issue #181) against the synthetic league
// (scripts/build_e2e_db.py). The fixture has no free-agent kicker or D/ST, so no week has enough projected free agents:
// every week must say why it is not planned, with no player and no number, and the current-week advice must still link
// back to the kicker and defense pages for week 3 (nothing in the fixture has kicked off). Driven from the keyboard on
// desktop-1440 and mobile-375.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const isPhone = () => /^mobile/.test(test.info().project.name);

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

const section = (page: Page) => page.locator("#kdst-streaming");

async function open(page: Page, query = "") {
  await page.goto(`/schedule-intelligence${query}`);
  await expect(section(page).getByRole("heading", { name: "K and D/ST streaming plan" })).toBeVisible({ timeout: 20_000 });
}

/** Start keyboard focus at the section heading, then Tab (at most `max` times) until the focused element matches. */
async function tabTo(page: Page, matches: () => boolean, max = 60): Promise<void> {
  await page.evaluate(() => {
    const h = document.querySelector<HTMLElement>("#kdst-streaming h2")!;
    h.tabIndex = -1;
    h.focus();
  });
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    if (await page.evaluate(matches)) return;
  }
  throw new Error("element never received keyboard focus");
}

test("thin weeks show no pick and no number, and the advice links back to the kicker page", async ({ page }) => {
  await open(page);
  await expect(page.getByTestId("stream-advice-k")).toContainText("Week 3: no plan. No free agent is projected for week 3");
  for (const pos of ["k", "dst"]) {
    for (const w of [3, 4, 5]) {
      const item = page.getByTestId(`stream-${pos}-week-${w}`);
      await expect(item).toContainText("Not planned");
      await expect(item.locator(".missing")).toHaveCount(1);         // a dash with its reason, never 0
      await expect(item.locator(".missing")).toHaveAttribute("title", /free agent/);
    }
  }
  await tabTo(page, () => document.activeElement?.textContent === "Open kicker projections for week 3");
  await expect(page.locator(":focus")).toHaveAttribute("href", "/kickers?week=3");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/kickers\?week=3$/, { timeout: 20_000 });
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  // The kicker page links back to the plan.
  await expect(page.getByRole("link", { name: "Multi-week streaming plan" })).toHaveAttribute("href", /\/schedule-intelligence.*#kdst-streaming$/);
});

test("the plan settings are keyboard operable and the timeline stays compact", async ({ page }) => {
  await open(page);
  await expect(page.getByTestId("stream-dst").locator("ol > li")).toHaveCount(3);
  await tabTo(page, () => (document.activeElement as HTMLSelectElement | null)?.name === "stream_weeks");
  await page.keyboard.press("ArrowUp");                      // 3 -> 2 (native select)
  await expect(page.locator('select[name="stream_weeks"]')).toHaveValue("2");
  await page.locator('select[name="stream_weeks"]').press("Enter");
  if (!/stream_weeks=2/.test(page.url())) {
    await tabTo(page, () => document.activeElement?.textContent === "Plan");
    await page.keyboard.press("Enter");
  }
  await expect(page).toHaveURL(/stream_weeks=2/, { timeout: 20_000 });
  await expect(page.getByTestId("stream-dst").locator("ol > li")).toHaveCount(2);
  await expect(page.getByTestId("stream-dst-week-4")).toContainText("Defense projections");
  await expect(page.getByTestId("stream-dst-week-4").getByRole("link")).toHaveAttribute("href", "/defenses?week=4");

  // Compact: no horizontal page scroll; on phones one short row per week, on desktop the weeks run across.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const boxes = await page.getByTestId("stream-k").locator("ol > li").evaluateAll(
    (els) => els.map((e) => { const r = e.getBoundingClientRect(); return { top: r.top, height: r.height }; }));
  for (const b of boxes) expect(b.height).toBeLessThan(isPhone() ? 160 : 200);
  if (isPhone()) expect(boxes[1].top).toBeGreaterThan(boxes[0].top);
  else expect(boxes[1].top).toBe(boxes[0].top);
});
